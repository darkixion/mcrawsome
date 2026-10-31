"""Real GTK file drops supply URI lists, sometimes without any plain text."""

from types import SimpleNamespace
from typing import Any

from mcrawsome.gui_drop import GtkFileDrop


def fixture_drop() -> tuple[GtkFileDrop, Any, Any, list[Any], list[Any]]:
    delivered: list[Any] = []
    finished: list[Any] = []
    handler = GtkFileDrop(delivered.append, lambda *args: finished.append(args))
    requested: list[Any] = []
    widget = SimpleNamespace(
        drag_get_data=lambda *args: requested.append(args),
        stop_emission_by_name=lambda signal: None,
        requested=requested,
    )
    context = SimpleNamespace(list_targets=lambda: ["text/html", "text/uri-list"])
    return handler, widget, context, delivered, finished


def test_native_drop_uses_uri_list_only_after_release_and_finishes_once() -> None:
    handler, widget, context, delivered, finished = fixture_drop()
    data = SimpleNamespace(
        get_target=lambda: "text/uri-list",
        get_uris=lambda: [
            "file:///clips/take%20%22A%22.mcraw",
            "file://localhost/clips/%E6%9D%B1%E4%BA%AC%2520.mcraw",
        ],
        get_text=lambda: None,
    )
    handler.received(widget, context, 20, 30, data, 0, 100)
    assert delivered == finished == []  # WebKit requests data during hover, too.
    assert handler.drop(widget, context, 20, 30, 101)
    assert widget.requested == [(context, "text/uri-list", 101)]
    handler.received(widget, context, 20, 30, data, 0, 102)
    assert delivered == [['/clips/take "A".mcraw', "/clips/東京%20.mcraw"]]
    assert finished == [(context, True, False, 102)]
    handler.received(widget, context, 20, 30, data, 0, 103)
    assert len(delivered) == len(finished) == 1


def test_native_drop_ignores_unrelated_data_and_refuses_remote_uris() -> None:
    handler, widget, context, delivered, finished = fixture_drop()
    assert not handler.drop(widget, SimpleNamespace(list_targets=lambda: ["text/plain"]), 0, 0, 1)
    assert handler.drop(widget, context, 0, 0, 2)
    data = SimpleNamespace(
        get_target=lambda: "text/uri-list",
        get_uris=lambda: [
            "https://example.com/video.mcraw",
            "file://other-computer/clips/video.mcraw",
            "file:relative.mcraw",
            "file:///clips/bad%00.mcraw",
        ],
    )
    handler.received(widget, object(), 0, 0, data, 0, 3)
    assert delivered == finished == []
    handler.received(widget, context, 0, 0, SimpleNamespace(get_target=lambda: "text/html"), 0, 3)
    assert delivered == finished == []
    handler.received(widget, context, 0, 0, data, 0, 4)
    assert delivered == []
    assert finished == [(context, False, False, 4)]
