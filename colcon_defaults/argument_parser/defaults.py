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
        with SuppressUsageOutput([self._parser] + list(all_parsers.values())):
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
        for key, dest in self.get_destinations(recursive=False).items():
            if key in data:
                defaults[dest] = wrap_default_value(data[key])

        if defaults:
            logger.info(
                "Setting default values for parser '{parser_name}': {defaults}"
                .format_map(locals()))
            self._parser.set_defaults(**defaults)
