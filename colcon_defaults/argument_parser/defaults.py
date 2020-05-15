# Copyright 2016-2020 Dirk Thomas
# Licensed under the Apache License, Version 2.0

import os
from pathlib import Path

from colcon_core.argument_default import wrap_default_value
from colcon_core.argument_parser import ArgumentParserDecoratorExtensionPoint
from colcon_core.argument_parser import SuppressUsageOutput
from colcon_core.argument_parser.destination_collector \
    import DestinationCollectorDecorator
from colcon_core.environment_variable import EnvironmentVariable
from colcon_core.location import get_config_path
from colcon_core.logging import colcon_logger
from colcon_core.plugin_system import satisfies_version
import yaml

logger = colcon_logger.getChild(__name__)

"""Environment variable to set the path to the default values"""
DEFAULTS_FILE_ENVIRONMENT_VARIABLE = EnvironmentVariable(
    'COLCON_DEFAULTS_FILE',
    'Set path to the yaml file containing the default values for the command '
    'line arguments (default: $COLCON_HOME/defaults.yaml)')


class DefaultArgumentsArgumentParserDecorator(
    ArgumentParserDecoratorExtensionPoint
):
    """Default command line arguments from global config file."""

    def __init__(self):  # noqa: D107
        super().__init__()
        satisfies_version(
            ArgumentParserDecoratorExtensionPoint.EXTENSION_POINT_VERSION,
            '^1.0')

    def decorate_argument_parser(self, *, parser):  # noqa: D102
        return DefaultArgumentsDecorator(parser)


class DefaultArgumentsDecorator(DestinationCollectorDecorator):
    """Provide custom default values for command line arguments."""

    def __init__(self, parser):  # noqa: D107
        # avoid setting members directly, the base class overrides __setattr__
        # pass them as keyword arguments instead
        super().__init__(
            parser,
            _config_path=Path(os.environ.get(
                DEFAULTS_FILE_ENVIRONMENT_VARIABLE.name,
                get_config_path() / 'defaults.yaml')),
            _parsers={},
            _subparsers=[],
            _argument_types={},
            _unique_types=set(),
            _registered_types={},
        )

    def add_parser(self, *args, **kwargs):
        """Collect association of subparsers to their name."""
        subparser = super().add_parser(
            *args, **kwargs)
        assert args[0] not in self._parsers
        self._parsers[args[0]] = subparser
        return subparser

    def add_subparsers(self, *args, **kwargs):
        """Collect all subparsers."""
        subparser = super().add_subparsers(*args, **kwargs)
        self._subparsers.append(subparser)
        return subparser

    def add_argument(self, *args, **kwargs):
        """Determine the type of the argument."""
        argument = super().add_argument(*args, **kwargs)

        type_ = None
        if kwargs.get('type') in (str, str.lstrip):
            type_ = str
        elif kwargs.get('type') == int:
            type_ = int
        elif kwargs.get('action') in ('store_false', 'store_true'):
            type_ = bool
        elif 'type' not in kwargs:
            type_ = str
        if kwargs.get('nargs') in ('*', '+'):
            type_ = (list, type_)
        self._argument_types[argument.dest] = type_

        if kwargs.get('type') is not None:
            self._unique_types.add(kwargs.get('type'))

        return argument

    def parse_args(self, *args, **kwargs):
        """Overwrite default values based on global configuration."""
        # mapping of all verbs to parsers
        def collect_parsers_by_verb(root, parsers, parent_verbs=()):
            for sp in root._subparsers:
                for name, p in sp._parsers.items():
                    verbs = parent_verbs + (name, )
                    parsers[verbs] = p
                    collect_parsers_by_verb(p, parsers, verbs)
        all_parsers = {}
        collect_parsers_by_verb(self, all_parsers)

        # collect passed verbs to determine relevant configuration options
        parsers_to_suppress = [self._parser] + list(all_parsers.values())
        with SuppressUsageOutput(parsers_to_suppress):
            with _SuppressTypeConversions(parsers_to_suppress):
                known_args, _ = self._parser.parse_known_args(*args, **kwargs)

        data = self._get_defaults_values(self._config_path)

        # determine data keys and parsers for passed verbs (including the root)
        keys_and_parsers = []
        nested_verbs = ()
        parser = self
        while True:
            keys_and_parsers.append(('.'.join(nested_verbs), parser))
            if len(parser._recursive_decorators) != 1:
                break
            if not hasattr(parser._recursive_decorators[0], 'dest'):
                break
            verb = getattr(
                known_args, parser._recursive_decorators[0].dest, None)
            if verb is None:
                break
            nested_verbs = nested_verbs + (verb, )
            parser = all_parsers[nested_verbs]

        for key, parser in keys_and_parsers:
            parser._set_parser_defaults(data.get(key, {}), parser_name=key)

        return self._parser.parse_args(*args, **kwargs)

    def register(self, *args, **kwargs):
        """Register a method, such as a type conversion or action."""
        if kwargs.get('registry_name', args[0]) == 'type':
            self._registered_types[kwargs.get('value', args[1])] = kwargs.get(
                'object', args[2])

    def _get_defaults_values(self, path):
        if not path.is_file():
            return {}

        content = path.read_text()
        data = yaml.safe_load(content)
        if data is None:
            logger.info(
                "Empty metadata file '%s'" % path.absolute())
            return {}

        if not isinstance(data, dict):
            logger.warning(
                "Skipping metadata file '%s' since it doesn't contain a dict" %
                path.absolute())
            return {}
        logger.info(
            "Using configuration from '%s'" % path.absolute())
        return data

    def _set_parser_defaults(self, data, *, parser_name):
        if not isinstance(data, dict):
            logger.warning(
                "Configuration option '{parser_name}' should be a dictionary"
                .format_map(locals()))
            return

        defaults = {}
        destinations = self.get_destinations(recursive=False)
        argument_types = self._get_argument_types()
        for key, dest in destinations.items():
            if key in data:
                value = data[key]
                type_ = argument_types.get(dest)
                if type_ is not None:
                    # check if the value has the expected type
                    try:
                        self._check_argument_type(
                            type_, value, key, parser_name)
                    except TypeError:
                        continue
                defaults[dest] = wrap_default_value(value)
        unknown_keys = data.keys() - destinations.keys()
        if unknown_keys:
            unknown_keys_str = ', '.join(sorted(unknown_keys))
            logger.warn(
                "Skipping unknown keys from '{self._config_path}' for "
                "'{parser_name}': {unknown_keys_str}".format_map(locals()))

        if defaults:
            logger.info(
                "Setting default values for parser '{parser_name}': {defaults}"
                .format_map(locals()))
            self._parser.set_defaults(**defaults)

    def _get_argument_types(self):
        argument_types = {}
        argument_types.update(self._argument_types)
        for d in self._group_decorators:
            argument_types.update(d._argument_types)
        return argument_types

    def _check_argument_type(self, type_, value, key, parser_name):
        if isinstance(type_, tuple) and type_[0] == list:
            if not isinstance(value, list):
                logger.warning(
                    "Default value '{key}' for parser '{parser_name}' should "
                    'be a list, not: {value}'.format_map(locals()))
                raise TypeError()
            # check type of each item in the list
            if type_[1] == int and any(not isinstance(v, int) for v in value):
                logger.warning(
                    "Default value '{key}' for parser '{parser_name}' should "
                    'be a list of integers, not: {value}'.format_map(locals()))
                raise TypeError()
            if type_[1] == str and any(not isinstance(v, str) for v in value):
                logger.warning(
                    "Default value '{key}' for parser '{parser_name}' should "
                    'be a list of strings, not: {value}'.format_map(locals()))
                raise TypeError()
        if type_ == bool and not isinstance(value, bool):
            logger.warning(
                "Default value '{key}' for parser '{parser_name}' should be a "
                'boolean, not: {value}'.format_map(locals()))
            raise TypeError()
        if type_ == int and not isinstance(value, int):
            logger.warning(
                "Default value '{key}' for parser '{parser_name}' should be "
                'an integer, not: {value}'.format_map(locals()))
            raise TypeError()
        if type_ == str and not isinstance(value, str):
            logger.warning(
                "Default value '{key}' for parser '{parser_name}' should be a "
                'string, not: {value}'.format_map(locals()))
            raise TypeError()


class _SuppressTypeConversions:
    """
    Context manager to suppress type conversions during `parse_known_args`.

    This works only with parsers decorated with `_unique_types` or
    `_registered_types`. It operates by registering a no-op conversion function
    (`str()`) in place of the original conversion, then restores the original
    conversion when exiting.
    """

    def __init__(self, parsers):
        """
        Construct a SuppressParseErrors.

        :param parsers: The parsers
        """
        self._parsers = parsers
        self._suppressed_types = {}

    def __enter__(self):
        for p in self._parsers:
            self._suppressed_types[p] = {}
            for t in getattr(p, '_unique_types', set()):
                p._parser.register('type', t, str)
                self._suppressed_types[p][t] = t
            for v, o in getattr(p, '_registered_types', {}).items():
                p._parser.register('type', v, str)
                self._suppressed_types[p][v] = o

    def __exit__(self, *args):
        for p, types in self._suppressed_types.items():
            for v, o in types.items():
                p.register('type', v, o)
