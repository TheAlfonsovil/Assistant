"""Observing a page as text, and deciding whether an action changed it.

The GUI loop only works if the observation is trustworthy: stable when nothing
changes, different when something does, and honest about what it cannot see.
These tests pin that down without needing a browser.
"""

from __future__ import annotations

import pytest

from assistant.devices.computer.page import (
    PageError,
    digest,
    find,
    matches,
    observe,
    parse_html,
    role_for,
    summarise,
)

PAGE = """
<!doctype html>
<html><head>
  <title>Panel de control</title>
  <style>.hidden { display: none }</style>
  <script>var noise = 'this must not appear in the text';</script>
</head><body>
  <h1>Resumen</h1>
  <p>Servicio operativo. Última revisión hace 2 minutos.</p>
  <form action="/save" method="post">
    <label for="name">Nombre</label>
    <input id="name" name="name" type="text" placeholder="Tu nombre" value="Alfonso" />
    <input type="checkbox" name="news" checked />
    <select name="env"><option value="prod">Producción</option></select>
    <textarea name="notes" aria-label="Notas"></textarea>
    <button type="submit">Guardar</button>
  </form>
  <a href="/dashboard/tasks">Tareas</a>
  <button disabled>Cancelar</button>
  <input type="text" style="display:none" name="ghost" value="invisible" />
  <div role="alert" aria-hidden="true">Error oculto</div>
</body></html>
"""


def test_roles_come_from_one_shared_mapping():
    assert role_for("input", "checkbox") == "checkbox"
    assert role_for("input", "submit") == "button"
    assert role_for("a") == "link"
    assert role_for("div", "", "alert") == "alert"
    assert role_for("div") is None


def test_a_page_is_observed_as_text_and_elements():
    observation = parse_html(PAGE, url="http://localhost/")

    assert observation["title"] == "Panel de control"
    assert "Servicio operativo" in observation["text"]
    assert "this must not appear" not in observation["text"]
    assert observation["source"] == "http"
    assert observation["coordinates"] is False

    elements = observation["elements"]
    roles = [item["role"] for item in elements]
    assert roles.count("textbox") == 2  # the text input and the textarea
    assert "button" in roles and "link" in roles and "checkbox" in roles

    by_value = {item["value"]: item for item in elements if item["value"]}
    assert by_value["Alfonso"]["name"] == "Tu nombre"
    assert by_value["Alfonso"]["role"] == "textbox"

    save_button = next(item for item in elements if item["name"] == "Guardar")
    assert save_button["role"] == "button"
    assert save_button["type"] == "submit"

    link = next(item for item in elements if item["role"] == "link")
    assert link["href"] == "/dashboard/tasks"
    assert link["name"] == "Tareas"

    disabled = next(item for item in elements if item["name"] == "Cancelar")
    assert disabled["disabled"] is True


def test_hidden_and_aria_hidden_content_is_left_out():
    observation = parse_html(PAGE)
    names = {item["name"] for item in observation["elements"]}

    assert "invisible" not in names
    assert "Error oculto" not in observation["text"]


def test_the_same_page_produces_the_same_digest():
    first = parse_html(PAGE, url="http://localhost/")
    second = parse_html(PAGE, url="http://localhost/")

    assert first["digest"] == second["digest"]


@pytest.mark.parametrize(
    "change",
    [
        PAGE.replace("Servicio operativo", "Servicio caído"),
        PAGE.replace(">Guardar<", ">Enviar<"),
        PAGE.replace('value="Alfonso"', 'value="Otra"'),
        PAGE.replace('href="/dashboard/tasks"', 'href="/dashboard/other"'),
    ],
)
def test_any_visible_change_changes_the_digest(change):
    assert parse_html(change, url="http://localhost/")["digest"] != parse_html(
        PAGE, url="http://localhost/"
    )["digest"]


def test_a_different_url_is_a_different_state():
    assert parse_html(PAGE, url="http://a/")["digest"] != parse_html(PAGE, url="http://b/")["digest"]


def test_the_digest_ignores_bookkeeping_fields():
    observation = parse_html(PAGE, url="http://localhost/")
    noisy = {
        **observation,
        "elements_total": 9999,
        "truncated": True,
        "text_truncated": True,
        "status_code": 200,
    }

    assert digest(noisy) == observation["digest"]


def test_elements_are_referenced_and_findable():
    observation = parse_html(PAGE)
    first = observation["elements"][0]

    assert first["ref"].startswith("e")
    assert find(observation, first["ref"])["name"] == first["name"]
    assert find(observation, "e999") is None


def test_a_summary_names_what_a_worker_can_act_on():
    summary = summarise(parse_html(PAGE, url="http://localhost/"))

    assert "digest=" in summary
    assert "Guardar" in summary
    assert "elements:" in summary


@pytest.mark.parametrize(
    ("predicate", "expected"),
    [
        ({"text_contains": "operativo"}, True),
        ({"text_contains": "caído"}, False),
        ({"title_contains": "control"}, True),
        ({"url_contains": "localhost"}, True),
        ({"element_present": {"role": "button", "name": "guardar"}}, True),
        ({"element_present": {"role": "button", "name": "inexistente"}}, False),
        ({"element_present": {"role": "spinbutton"}}, False),
        ({}, True),
        (None, False),
    ],
)
def test_predicates_are_deterministic(predicate, expected):
    assert matches(parse_html(PAGE, url="http://localhost/"), predicate) is expected


def test_a_digest_change_predicate_needs_a_real_change():
    observation = parse_html(PAGE, url="http://localhost/")

    assert matches(
        observation, {"digest_changed": True, "previous_digest": observation["digest"]}
    ) is False
    assert matches(
        observation, {"digest_changed": True, "previous_digest": "something-else"}
    ) is True


@pytest.mark.asyncio
async def test_observation_without_a_surface_is_an_error():
    # No devtools port and no url to fetch: saying so beats inventing a state.
    with pytest.raises(PageError) as error:
        await observe(timeout=0.5)

    assert "no url" in str(error.value) or "no debuggable" in str(error.value)


@pytest.mark.asyncio
async def test_observation_can_fall_back_to_a_plain_fetch():
    server = _serve(PAGE, title="Panel de control")
    try:
        observation = await observe(url=server, timeout=5.0)
    finally:
        _stop = getattr(server, "close", None)
        if _stop:
            _stop()

    assert observation["source"] == "http"
    assert observation["status_code"] == 200
    assert "Servicio operativo" in observation["text"]
    assert observation["digest"]


def _serve(html: str, title: str = "") -> str:
    """Start a tiny local server and return its url."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.close = server.shutdown  # type: ignore[attr-defined]
    return f"http://127.0.0.1:{server.server_port}/"
