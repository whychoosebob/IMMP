"""
Backbone for other hooks to process commands contained in channel messages.

Config:
    prefix (str):
        Characters at the start of a message to denote commands.  Use a single character to
        make commands top-level (e.g. ``"?"`` would allow commands like ``?help``), or a string
        followed by a space for subcommands (e.g. ``"!bot "`` for ``!bot help``).
    return-errors (bool):
        ``True`` to send unhandled exceptions raised by commands back to the source channel
        (``False`` by default).
    sets ((str, str list) dict):
        Subsets of hook commands by name, to restrict certain features.
    groups (str, dict) dict):
        Named config groups to enable commands in selected channels.

        plugs ((str, str list) dict):
            List of plugs where commands should be processed in **private** or **shared**
            (non-private) channels, or **anywhere**.
        channels (str list):
            List of channels to process public commands in (independent of *plugs* above).
        hooks (str list):
            List of hooks to enable commands for.
        sets (str list):
            List of command sets to enable.
        admins ((str, str list) dict):
            Users authorised to execute administrative commands, a mapping of network identifiers
            to lists of user identifiers.

The binding works by making commands exposed by all listed hooks available to all listed channels,
and to the private channels of all listed plugs.  Note that the channels need not belong to any
hook-specific config -- you can, for example, bind some commands to an admin-only channel
elsewhere.  Multiple groups can be used for fine-grained control.
"""

from enum import Enum
import inspect
import logging
import shlex

from voluptuous import ALLOW_EXTRA, Any, Optional, Schema

import immp


log = logging.getLogger(__name__)


class _Schema:

    def _key(name, default=dict):
        return Optional(name, default=default)

    config = Schema({"prefix": [str],
                     _key("return-errors", False): bool,
                     _key("sets"): Any({}, {str: {str: [str]}}),
                     "groups": {str: {_key("plugs"): {_key("anywhere", list): [str],
                                                      _key("private", list): [str],
                                                      _key("shared", list): [str]},
                                      _key("channels", list): [str],
                                      _key("hooks", list): [str],
                                      _key("sets", list): [str],
                                      _key("admins"): Any({}, {str: [str]})}}},
                    extra=ALLOW_EXTRA, required=True)


class BadUsage(immp.HookError):
    """
    May be raised from within a command to indicate that the arguments were invalid.
    """


class CommandParser(Enum):
    """
    Constants representing the method used to parse the argument text following a used command.

    Attributes:
        spaces:
            Split using :meth:`str.split`, for simple inputs breaking on whitespace characters.
        shlex:
            Split using :func:`shlex.split`, which allows quoting of multi-word arguments.
        none:
            Don't split the argument, just provide a single string.
    """
    spaces = 0
    shlex = 1
    none = 2


class CommandScope(Enum):
    """
    Constants representing the types of conversations a command is available in.

    Attributes:
        anywhere:
            All configured channels.
        private:
            Only private channels, as configured per-plug.
        shared:
            Only non-private channels, as configured per-channel.
    """
    anywhere = 0
    private = 1
    shared = 2


class CommandRole(Enum):
    """
    Constants representing the types of users a command is available in.

    Attributes:
        anyone:
            All configured channels.
        admin:
            Only authorised users in the command group.
    """
    anyone = 0
    admin = 1


@immp.pretty_str
class BoundCommand:
    """
    Wrapper object returned when accessing a command via a :class:`.Hook` instance, similar to
    :class:`types.MethodType`.

    This object is callable, which invokes the command's underlying method against the bound hook.
    """

    def __init__(self, hook, cmd):
        self.hook = hook
        self.cmd = cmd

    def test(self):
        """
        Run the custom test predicate against the bound hook.

        Returns:
            bool:
                ``True`` if the hook elects to support this command.
        """
        return self.cmd.test(self.hook) if self.cmd.test else True

    def applicable(self, private, admin):
        """
        Test the availability of the current command based on the scope and role.

        Returns:
            bool:
                ``True`` if the command may be used.
        """
        if self.scope == CommandScope.private and not private:
            return False
        elif self.scope == CommandScope.shared and private:
            return False
        if self.role == CommandRole.admin and not admin:
            return False
        if self.test:
            return bool(self.test())
        else:
            return True

    async def __call__(self, msg, *args):
        return await self.cmd.fn(self.hook, msg, *args)

    def __getattr__(self, name):
        # Propagate other attribute access to the unbound Command object.
        return getattr(self.cmd, name)

    def __repr__(self):
        return "<{}: {} {}>".format(self.__class__.__name__, repr(self.hook), repr(self.cmd))


@immp.pretty_str
class Command:
    """
    Container of a command function.  Use the :meth:`command` decorator to wrap a class method and
    convert it into an instance of this class.

    Accessing an instance of this class via the attribute of a containing class will create a new
    :class:`BoundCommand` allowing invocation of the method.

    Attributes:
        name (str):
            Command name, used to access the command when directly following the prefix.
        fn (method):
            Callback function to process the command usage.
        parser (.CommandParser):
            Parse mode for the command arguments.
        scope (.CommandScope):
            Accessibility of this command for the different channel types.
        role (.CommandRole):
            Accessibility of this command for different users.
        test (method):
            Additional predicate that can enable or disable a command based on hook state.
        doc (str):
            Full description of the command.
        spec (str):
            Readable summary of accepted arguments, e.g. ``<required> [optional] [varargs...]``.
    """

    def __init__(self, name, fn, parser=CommandParser.spaces, scope=CommandScope.anywhere,
                 role=CommandRole.anyone, test=None):
        self.name = name.lower()
        self.fn = fn
        self.parser = parser
        self.scope = scope
        self.role = role
        self.test = test
        # Since users are providing arguments parsed by shlex.split(), there are no keywords.
        if any(param.kind in (inspect.Parameter.KEYWORD_ONLY,
                              inspect.Parameter.VAR_KEYWORD) for param in self._args):
            raise ValueError("Keyword-only command parameters are not supported: {}".format(fn))

    def __get__(self, instance, owner):
        return BoundCommand(instance, self) if instance else self

    @property
    def _args(self):
        sig = inspect.signature(self.fn)
        # Skip self and msg arguments.
        return tuple(sig.parameters.values())[2:]

    @property
    def doc(self):
        return inspect.cleandoc(self.fn.__doc__) if self.fn.__doc__ else None

    @property
    def spec(self):
        parts = []
        for param in self._args:
            if param.kind in (inspect.Parameter.POSITIONAL_ONLY,
                              inspect.Parameter.POSITIONAL_OR_KEYWORD):
                parts.append(("<{}>" if param.default is inspect.Parameter.empty else "[{}]")
                             .format(param.name))
            elif param.kind == inspect.Parameter.VAR_POSITIONAL:
                parts.append("[{}...]".format(param.name))
        return " ".join(parts)

    def parse(self, args):
        """
        Convert a string of multiple arguments into a list according to the chosen parse mode.

        Args:
            args (str):
                Raw arguments from a message.

        Returns:
            str list:
                Parsed arguments.
        """
        if not args:
            return []
        if self.parser == CommandParser.spaces:
            return args.split()
        elif self.parser == CommandParser.shlex:
            return shlex.split(args)
        elif self.parser == CommandParser.none:
            return [args]

    def valid(self, *args):
        """
        Test the validity of the given arguments against the command's underlying method.  Raises
        :class:`ValueError` if the arguments don't match the signature.

        Args:
            args (str list):
                Parsed arguments.
        """
        params = self._args
        required = len([arg for arg in params if arg.default is inspect.Parameter.empty])
        varargs = len([arg for arg in params if arg.kind is inspect.Parameter.VAR_POSITIONAL])
        required -= varargs
        if len(args) < required:
            raise ValueError("Expected at least {} args, got {}".format(required, len(args)))
        if len(args) > len(params) and not varargs:
            raise ValueError("Expected at most {} args, got {}".format(len(params), len(args)))

    def __repr__(self):
        return "<{}: {} @ {}, {} {}>".format(self.__class__.__name__, self.name, self.scope.name,
                                             self.role.name, self.fn)


def command(name, parser=CommandParser.spaces, scope=CommandScope.anywhere,
            role=CommandRole.anyone, test=None):
    """
    Decorator: mark up the method as a command.

    This doesn't return the original function, rather a :class:`.Command` object.

    Arguments:
        name (str):
            Command name, used to access the command when directly following the prefix.
        parser (.CommandParser):
            Parse mode for the command arguments.
        scope (.CommandScope):
            Accessibility of this command for the different channel types.
        role (.CommandRole):
            Accessibility of this command for different users.
        test (method):
            Additional predicate that can enable or disable a command based on hook state.
    """
    return lambda fn: Command(name, fn, parser, scope, role, test)


class CommandHook(immp.Hook):
    """
    Generic command handler for other hooks.
    """

    def __init__(self, name, config, host):
        super().__init__(name, _Schema.config(config), host)

    def _hook(self, name):
        try:
            return self.host.hooks[name]
        except KeyError:
            for hook in self.host.resources.values():
                if hook.name == name:
                    return hook
            raise

    def discover(self, hook):
        """
        Inspect a :class:`.Hook` instance, scanning its attributes for commands.

        Returns:
            (str, .BoundCommand) dict:
                Commands provided by this hook, keyed by name.
        """
        if hook.state != immp.OpenState.active:
            return {}
        attrs = [getattr(hook, attr) for attr in dir(hook)]
        return {cmd.name: cmd for cmd in attrs if isinstance(cmd, BoundCommand)}

    async def commands(self, channel, user):
        """
        Retrieve all commands, and filter against the group config based on the containing channel
        and user.

        Returns:
            (str, .BoundCommand) dict:
                Commands provided by all hooks, in this channel for this user, keyed by name.
        """
        private = await channel.is_private()
        groups = []
        for group in self.config["groups"].values():
            anywhere = group["plugs"]["anywhere"]
            if channel.plug.name in anywhere + group["plugs"]["private"] and private:
                groups.append(group)
            elif channel.plug.name in anywhere + group["plugs"]["shared"] and not private:
                groups.append(group)
            elif any(channel == self.host.channels[label] for label in group["channels"]):
                groups.append(group)
        cmds = set()
        for group in groups:
            cmdgroup = set()
            admin = user.plug and user.id in (group["admins"].get(user.plug.name) or [])
            for name in group["hooks"]:
                cmdgroup.update(set(self.discover(self._hook(name)).values()))
            for label in group["sets"]:
                for name, cmdset in self.config["sets"][label].items():
                    discovered = self.discover(self._hook(name))
                    cmdgroup.update(set(discovered[cmd] for cmd in cmdset))
            cmds.update(cmd for cmd in cmdgroup if cmd.applicable(private, admin))
        mapped = {cmd.name: cmd for cmd in cmds}
        if len(cmds) > len(mapped):
            # Mapping by name silently overwrote at least one command with a duplicate name.
            raise immp.ConfigError("Multiple applicable commands named '{}'".format(name))
        return mapped

    @command("help")
    async def help(self, msg, command=None):
        """
        List all available commands in this channel, or show help about a single command.
        """
        cmds = await self.commands(msg.channel, msg.user)
        if command:
            try:
                cmd = cmds[command]
            except KeyError:
                text = "\N{CROSS MARK} No such command"
            else:
                text = immp.RichText([immp.Segment(cmd.name, bold=True)])
                if cmd.spec:
                    text.append(immp.Segment(" {}".format(cmd.spec)))
                if cmd.doc:
                    text.append(immp.Segment(":", bold=True),
                                immp.Segment("\n{}".format(cmd.doc)))
        else:
            text = immp.RichText([immp.Segment("Available commands:", bold=True)])
            for name, cmd in sorted(cmds.items()):
                text.append(immp.Segment("\n- {}".format(name)))
                if cmd.spec:
                    text.append(immp.Segment(" {}".format(cmd.spec), italic=True))
        await msg.channel.send(immp.Message(text=text))

    async def on_receive(self, sent, source, primary):
        await super().on_receive(sent, source, primary)
        if not primary or not sent.user or not sent.text:
            return
        plain = str(sent.text)
        for prefix in self.config["prefix"]:
            if plain.lower().startswith(prefix):
                # TODO: Preserve formatting.
                raw = plain[len(prefix):].split(maxsplit=1)
                break
        else:
            return
        name = raw[0].lower()
        trailing = raw[1] if len(raw) == 2 else None
        cmds = await self.commands(sent.channel, sent.user)
        try:
            cmd = cmds[name]
        except KeyError:
            log.debug("No matches for command name '{}' in {}".format(name, repr(sent.channel)))
            return
        else:
            log.debug("Matched command in {}: {}".format(repr(sent.channel), repr(cmd)))
        try:
            args = cmd.parse(trailing)
            cmd.valid(*args)
        except ValueError:
            # Invalid number of arguments passed, return the command usage.
            await self.help(sent, name)
            return
        try:
            log.debug("Executing command: {} {}".format(repr(sent.channel), sent.text))
            await cmd(sent, *args)
        except BadUsage:
            await self.help(sent, name)
        except Exception as e:
            log.exception("Exception whilst running command: {}".format(sent.text))
            if self.config["return-errors"]:
                text = ": ".join(filter(None, (e.__class__.__name__, str(e))))
                await sent.channel.send(immp.Message(text="\N{WARNING SIGN} {}".format(text)))
