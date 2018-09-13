# Copyright 2016-2018 Dirk Thomas
# Licensed under the Apache License, Version 2.0

import os
from pathlib import Path

from colcon_core.argument_parser import ArgumentParserDecoratorExtensionPoint
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
        )

    def add_parser(self, *args, **kwargs):
        """Collect association of subparsers to their name."""
        subparser = super().add_parser(
            *args, **kwargs)
        assert args[0] not in self._parsers
        self._parsers[args[0]] = subparser
        return subparser

    def parse_args(self, *args, **kwargs):
        """Overwrite default values based on global configuration."""
        data = self._get_defaults_values(self._config_path)
        self._filter_valid_default_values(data)
        logger.debug('Setting default values: {data}'.format_map(locals()))
        self._set_parser_defaults(data)
        return self._parser.parse_args(*args, **kwargs)

    def _get_defaults_values(self, path):
        if not path.is_file():
            return {}

        content = path.read_text()
        data = yaml.load(content)
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

    def _filter_valid_default_values(self, data, group=None):
        for k in sorted(data.keys()):
            name = k
            if group is not None:
                name = group + '.' + name

            if k in self.get_destinations().keys():
                continue
            for d in self._nested_decorators:
                if k in d.get_destinations().keys():
                    break
                if k in d._parsers:
                    v = data[k]
                    if not isinstance(v, dict):
                        logger.warning(
                            "Configuration option '%s' should be a dictionary",
                            name)
                        del data[k]
                    else:
                        d._parsers[k]._filter_valid_default_values(
                            data[k], group=k)
                    break
            else:
                # ignore unknown configuration option
                del data[k]

    def _set_parser_defaults(self, data):
        defaults = {}
        # collect defaults for all arguments known to this parser
        for argument_name, destination in self.get_destinations().items():
            if argument_name in data:
                defaults[destination] = data[argument_name]
        # also consider nested parsers like groups
        for d in self._nested_decorators:
            for argument_name, destination in d.get_destinations().items():
                if argument_name in data:
                    defaults[destination] = data[argument_name]
        self._parser.set_defaults(**defaults)

        # set defaults on all nested parsers based on their prefix
        for d in self._nested_decorators:
            for prefix, parser in d._parsers.items():
                if prefix in data:
                    parser._set_parser_defaults(data[prefix])
