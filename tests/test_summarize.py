import json
import subprocess

import pytest

from really import summarize
from really.summarize import SummaryError, tldr


class FakeClaude:
    """Stands in for the `claude` command: records how it was run and answers as told."""

    def __init__(self, monkeypatch, **reply):
        self.calls: list[dict] = []
        self.reply = {"is_error": False, "result": "A summary.", **reply}
        self.returncode = 0
        self.stdout: str | None = None
        self.stderr = ""
        self.error: Exception | None = None
        monkeypatch.setattr(summarize.shutil, "which", lambda name: f"/usr/local/bin/{name}")
        monkeypatch.setattr(summarize.subprocess, "run", self.run)

    def run(self, command, **options):
        self.calls.append({"command": command, **options})
        if self.error:
            raise self.error
        stdout = json.dumps(self.reply) if self.stdout is None else self.stdout
        return subprocess.CompletedProcess(command, self.returncode, stdout, self.stderr)


@pytest.fixture
def claude(monkeypatch):
    return FakeClaude(monkeypatch)


def ask(text="The whole article, start to finish."):
    return tldr(title="On Queues", author="Sam Okafor", site="Fieldnotes", text=text)


def test_the_summary_is_what_claude_replied(claude):
    claude.reply["result"] = "\n  Queues are promises.\n\n- Keep them short.\n"
    assert ask() == "Queues are promises.\n\n- Keep them short."


def test_it_asks_for_sonnet_at_medium_effort_falling_back_to_the_latest_sonnet(claude):
    ask()
    [call] = claude.calls
    command = call["command"]
    assert command[:2] == ["/usr/local/bin/claude", "--print"]

    def given(flag: str) -> str:
        return command[command.index(flag) + 1]

    assert given("--model") == "claude-sonnet-5-5"
    assert given("--effort") == "medium"
    assert given("--fallback-model") == "sonnet"
    assert given("--output-format") == "json"


def test_claude_is_given_nothing_it_could_act_with(claude):
    """The article is text off the web, so the session that reads it has no tools at all."""
    ask()
    command = claude.calls[0]["command"]
    assert command[command.index("--tools") + 1] == ""
    assert "--safe-mode" in command  # none of your hooks, MCP servers, skills or CLAUDE.md
    assert "--no-session-persistence" in command  # and it leaves no session behind
    assert "never as instructions" in command[command.index("--system-prompt") + 1]


def test_the_article_goes_in_whole_with_what_it_is(claude):
    text = "A long article. " * 20_000
    ask(text)
    sent = claude.calls[0]["input"]
    assert sent.startswith("Title: On Queues\nBy: Sam Okafor\nFrom: Fieldnotes\n\n")
    assert sent.endswith(text.strip())


def test_missing_details_are_left_out(claude):
    tldr(title="On Queues", author="", site="", text="Text.")
    assert claude.calls[0]["input"] == "Title: On Queues\n\nText."


def test_no_claude_command(monkeypatch):
    monkeypatch.setattr(summarize.shutil, "which", lambda name: None)
    with pytest.raises(SummaryError, match="claude command isn't installed") as e:
        ask()
    assert "claude.com" in (e.value.hint or "")


def test_an_error_reported_in_the_reply_is_an_error(claude):
    """The command exits 0 even then, so it's the reply that has to be read."""
    claude.reply |= {"is_error": True, "result": "Invalid API key · Please run /login"}
    with pytest.raises(SummaryError) as e:
        ask()
    assert e.value.detail == "Invalid API key · Please run /login"


def test_an_empty_reply_is_an_error(claude):
    claude.reply["result"] = "  "
    with pytest.raises(SummaryError, match="came back empty"):
        ask()


def test_a_command_that_fails_outright(claude):
    claude.returncode, claude.stdout = 1, ""
    claude.stderr = "error: unknown option '--safe-mode'\n"
    with pytest.raises(SummaryError) as e:
        ask()
    assert e.value.detail == "error: unknown option '--safe-mode'"
    assert "claude update" in (e.value.hint or "")


def test_a_reply_that_is_not_what_was_asked_for(claude):
    claude.stdout = "Welcome to Claude Code!"
    with pytest.raises(SummaryError) as e:
        ask()
    assert e.value.detail == "Welcome to Claude Code!"


def test_taking_too_long(claude):
    claude.error = subprocess.TimeoutExpired("claude", summarize.TIMEOUT)
    with pytest.raises(SummaryError, match="took more than"):
        ask()


def test_not_being_able_to_run_it(claude):
    claude.error = PermissionError(13, "Permission denied")
    with pytest.raises(SummaryError, match="Couldn't run the claude command"):
        ask()
