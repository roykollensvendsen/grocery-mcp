"""The MCP server as the voice sees it, and the command line, against the fake."""

import asyncio
import datetime as dt

import httpx

from kassalapp_mcp.answers import PriceAnswers
from kassalapp_mcp.cli import main
from kassalapp_mcp.fake import FAKE_KEY, FakeKassalapp
from kassalapp_mcp.server import FIRST_LINE_MAX, build_server
from kassalapp_mcp.source import KassalappSource

TODAY = dt.date(2026, 11, 14)


def _server(fake=None, *, simulated=False):
    fake = fake or FakeKassalapp(today=TODAY)
    answers = PriceAnswers(KassalappSource(fake.client(), lambda: FAKE_KEY), today=lambda: TODAY)
    return build_server(answers, simulated=simulated)


def _tools(server):
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def _call(server, name, arguments):
    return asyncio.run(server.call_tool(name, arguments)).structured_content


def test_the_voice_sees_the_price_tool_marked_read_only():
    tools = _tools(_server())
    assert tools["grocery_price"].annotations.read_only_hint is True


def test_the_tool_opens_with_a_whole_sentence_the_app_shows_uncut():
    first = _tools(_server())["grocery_price"].description.splitlines()[0]
    assert len(first) <= FIRST_LINE_MAX
    assert first.endswith(("?", "."))


def test_the_tool_answers_with_the_sentence_to_read_aloud():
    answer = _call(_server(), "grocery_price", {"product": "fjellmelk lett", "chain": "Meny"})
    assert answer["say"].startswith("Fjellmelk lett 1 l costs 26.90 at Meny, checked today.")


def test_a_simulated_answer_says_so():
    server = _server(simulated=True)
    answer = _call(server, "grocery_price", {"product": "fjellmelk lett"})
    assert answer["say"].startswith("Simulated: ")
    assert answer["simulated"] is True
    assert server.instructions.startswith("SIMULATED")


def test_every_tool_answers_with_a_sentence_even_when_it_fails():
    def unreachable(request):
        message = "no network"
        raise httpx.ConnectError(message, request=request)

    fake = FakeKassalapp(today=TODAY)
    fake.transport = lambda: httpx.MockTransport(unreachable)
    answer = _call(_server(fake), "grocery_price", {"product": "fjellmelk lett"})
    assert answer["success"] is False
    assert answer["say"] == "Kassalapp did not answer; try again in a minute."


def test_the_command_line_answers_one_price_question_from_the_simulation(capsys):
    assert main(["price", "fjellmelk lett", "--chain", "Meny", "--simulate"]) == 0
    assert capsys.readouterr().out.startswith("Simulated: Fjellmelk lett 1 l costs 26.90 at Meny")


def test_search_words_are_never_logged(caplog):
    caplog.set_level("DEBUG")
    _call(_server(), "grocery_price", {"product": "fjellmelk lett"})
    assert "fjellmelk" not in caplog.text
    assert "7000000000017" not in caplog.text


def test_the_voice_sees_the_cheapest_tool_marked_read_only():
    tool = _tools(_server())["grocery_cheapest"]
    assert tool.annotations.read_only_hint is True
    assert len(tool.description.splitlines()[0]) <= FIRST_LINE_MAX


def test_the_two_tool_names_do_not_sound_alike():
    names = sorted(_tools(_server()))
    assert names == ["grocery_cheapest", "grocery_price"]
    assert names[0].split("_")[1][:3] != names[1].split("_")[1][:3]
