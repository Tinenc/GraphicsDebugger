"""Every uex recipe has to be a command uex actually accepts.

The first version of these recipes put ``--profile`` before the subcommand. uex
declares ``--config`` globally but ``--profile`` on each subcommand, so every
call failed with an unrecognised option, and the AI -- correctly -- concluded the
tool was broken and stopped using it. Nothing in a unit test caught that, because
a recipe is just a list of strings until something runs it.

So this reads the CLI out of uex's own ``Program.cs`` (the vendored source in the
bundle) and checks each recipe against it: global options before the subcommand,
declared options after it, and no more positional arguments than the subcommand
takes. It skips when the source is not there, which is every checkout that has
not run Fetch-Unpackers yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from replaylab_agent import agent_asset_readers, builtin_unpackers

_PROGRAM = (Path(__file__).resolve().parents[1] / "third_party" / "unpackers" / "_src" /
            "uex" / "src" / "Uex" / "Program.cs")


def _cli() -> tuple[set[str], dict[str, dict[str, list[str]]]]:
    source = _PROGRAM.read_text(encoding="utf-8", errors="replace")
    spelling = {match.group(1): match.group(3) for match in
                re.finditer(r'var (\w+) = new (Option|Argument)<[^>]+>\("([^"]+)"', source)}
    recursive = {spelling[match.group(1)] for match in
                 re.finditer(r'var (\w+) = new Option<[^>]+>\("[^"]+"\)\s*\{[^}]*Recursive\s*=\s*true',
                             source)}
    commands: dict[str, dict[str, list[str]]] = {}
    for match in re.finditer(r'var (\w+Command) = new Command\(\s*"([^"]+)"', source):
        variable, name = match.group(1), match.group(2)
        block = source[match.end(): match.end() + 2500]
        options: list[str] = []
        arguments: list[str] = []
        for added in re.finditer(r'%s\.(Options|Arguments)\.Add\((\w+)\)' % variable, block):
            target = options if added.group(1) == "Options" else arguments
            target.append(spelling.get(added.group(2), added.group(2)))
        commands[name] = {"options": options, "arguments": arguments}
    return recursive, commands


@pytest.mark.skipif(not _PROGRAM.is_file(), reason="the vendored uex source is not in this checkout")
def test_every_uex_recipe_is_a_command_uex_accepts() -> None:
    recursive, commands = _cli()
    assert "--config" in recursive, "uex's global options were not recognised in Program.cs"
    assert {"preview", "list", "search", "discover"} <= set(commands)

    definition = builtin_unpackers.builtin_unpacker_definition("uex", "uex.exe", {})
    problems: list[str] = []
    for operation in definition["interface"]["operations"]:
        argv = list(operation["argv"])
        index = 0
        while index < len(argv) and argv[index].startswith("--"):
            if argv[index] not in recursive:
                problems.append("%s: %s is used before the subcommand but is not global"
                                % (operation["name"], argv[index]))
            index += 2
        if index >= len(argv):
            problems.append("%s: no subcommand" % operation["name"])
            continue
        subcommand = argv[index]
        index += 1
        if subcommand not in commands:
            problems.append("%s: unknown subcommand %r" % (operation["name"], subcommand))
            continue
        declared = commands[subcommand]
        positional = 0
        while index < len(argv):
            token = argv[index]
            if token.startswith("--"):
                if token not in declared["options"] and token not in recursive:
                    problems.append("%s: uex %s does not accept %s"
                                    % (operation["name"], subcommand, token))
                index += 2
            else:
                positional += 1
                index += 1
        if positional > len(declared["arguments"]):
            problems.append("%s: uex %s takes %d positional argument(s), %d given"
                            % (operation["name"], subcommand, len(declared["arguments"]), positional))
        # Every subcommand declares --profile, so every recipe has to pass it there.
        assert "--profile" in declared["options"], subcommand
        assert "--profile" in argv[argv.index(subcommand):], (
            "%s must pass --profile after the subcommand" % operation["name"])
    assert not problems, "uex recipes do not match uex's CLI:\n  " + "\n  ".join(problems)


@pytest.mark.skipif(not _PROGRAM.is_file(), reason="the vendored uex source is not in this checkout")
def test_settings_reader_recipes_also_follow_the_actual_uex_cli() -> None:
    recursive, commands = _cli()
    preset = next(item for item in agent_asset_readers.PRESETS if item["id"] == "uex")
    tool = agent_asset_readers.recognise_tool("uex.exe")
    recipes = [preset["args"], preset["probeArgs"], *tool["invocations"],
               agent_asset_readers.registration_from_command("uex.exe")["args"]]
    for recipe in recipes:
        argv = list(recipe)
        index = 0
        while index < len(argv) and argv[index].startswith("--"):
            assert argv[index] in recursive, recipe
            assert index + 1 < len(argv), recipe
            index += 2
        assert index < len(argv) and argv[index] == "preview", recipe
        declared = commands[argv[index]]
        index += 1
        positional = 0
        while index < len(argv):
            if argv[index].startswith("--"):
                assert argv[index] in declared["options"] or argv[index] in recursive, recipe
                assert index + 1 < len(argv), recipe
                index += 2
            else:
                positional += 1
                index += 1
        assert positional == len(declared["arguments"]), recipe
