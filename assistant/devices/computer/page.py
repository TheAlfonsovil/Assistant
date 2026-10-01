"""What the observable surfaces look like right now.

An agent that clicks and types needs to *see* the result, and a text model
cannot look at pixels. This module turns a page into a stable, structured
observation — url, title, visible text, interactive elements with a reference
each — so a worker can decide the next action, and a verifier can tell whether
the action changed anything.

Two sources feed the same shape:

``cdp``
    A Chromium tab with remote debugging enabled. Live DOM, real element boxes,
    and coordinates that can be clicked. Requires ``websockets``.
``http``
    A plain fetch plus a stdlib HTML parse. No layout and no JavaScript, but it
    needs no dependency, no debugging port and no browser at all.

The ``digest`` is the state fingerprint: identical digests mean the surface did
not change, which is what turns "I clicked and it looked fine" into a measurable
fact and what stops a loop from repeating an action forever.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any

# Observation budgets. Sized for a large-window model: a page read that is too
# small forces a second round-trip (and a second LLM turn) to see what was cut,
# which costs more than carrying the extra characters once.
MAX_TEXT_CHARS = 12_000
MAX_ELEMENTS = 120
MAX_NAME_CHARS = 160

_SKIP_TEXT = {"script", "style", "noscript", "template", "svg", "head"}
_VOID = {"br", "img", "input", "meta", "link", "hr", "source", "track", "area", "col"}
_WS = re.compile(r"\s+")

# Elements a worker can meaningfully act on, keyed by tag.
_INTERACTIVE = {
    "a": "link",
    "button": "button",
    "input": "input",
    "select": "select",
    "textarea": "textbox",
    "summary": "disclosure",
    "option": "option",
    "label": "label",
}

# Inline CSS is the only layout hint available without a rendering engine, but it
# catches the common "present in the DOM, invisible to the user" case.
_INLINE_HIDDEN = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?![.\d])",
    re.IGNORECASE,
)

_INPUT_ROLES = {
    "text": "textbox",
    "search": "searchbox",
    "email": "textbox",
    "password": "textbox",
    "url": "textbox",
    "tel": "textbox",
    "number": "spinbutton",
    "checkbox": "checkbox",
    "radio": "radio",
    "range": "slider",
    "file": "file",
    "submit": "button",
    "button": "button",
    "reset": "button",
    "color": "color",
    "date": "date",
    "time": "time",
}


def collapse(value: str, limit: int = MAX_NAME_CHARS) -> str:
    """One line, trimmed, and never longer than the budget."""
    text = _WS.sub(" ", value or "").strip()
    return text[:limit]


# Injected into the page by the CDP source. It returns the same shape the HTML
# parser produces, plus real element boxes, so both sources share one digest and
# one prompt rendering. ``data-assistant-ref`` makes a later click by reference
# stable while the page has not navigated.
OBSERVATION_SCRIPT = r"""
(() => {
  const selector = 'a[href],button,input,select,textarea,summary,option,' +
    '[role],[onclick],[contenteditable],[tabindex]';
  const visible = (el) => {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const label = (el) => (el.getAttribute('aria-label') || el.value || el.placeholder ||
    el.title || el.alt || el.innerText || el.name || '').trim().slice(0, 120);
  const nodes = [...document.querySelectorAll(selector)].filter(visible).slice(0, 400);
  const elements = nodes.map((el, i) => {
    const ref = 'e' + (i + 1);
    el.setAttribute('data-assistant-ref', ref);
    const box = el.getBoundingClientRect();
    return {
      ref, tag: el.tagName.toLowerCase(), type: el.type || '',
      role_attr: el.getAttribute('role') || '', name: label(el),
      value: (el.value === undefined || el.value === null ? '' : String(el.value)).slice(0, 120),
      disabled: !!el.disabled, href: el.href || '',
      rect: {
        x: Math.round(box.left + window.scrollX),
        y: Math.round(box.top + window.scrollY),
        width: Math.round(box.width), height: Math.round(box.height),
      },
    };
  });
  return {
    source: 'cdp', url: location.href, title: document.title,
    text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 6000),
    elements, elements_total: nodes.length, coordinates: true,
    viewport: {width: innerWidth, height: innerHeight, scroll_y: Math.round(window.scrollY)},
  };
})()
"""

# Viewport-relative centre of one referenced element: CDP input events use
# viewport coordinates, while the observation reports document coordinates.
CLICK_POINT_SCRIPT = r"""
(() => {
  const el = document.querySelector('[data-assistant-ref="%s"]');
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const box = el.getBoundingClientRect();
  if (box.width === 0 && box.height === 0) return null;
  return {x: Math.round(box.left + box.width / 2), y: Math.round(box.top + box.height / 2)};
})()
"""

FOCUS_SCRIPT = r"""
(() => {
  const el = document.querySelector('[data-assistant-ref="%s"]');
  if (!el) return false;
  el.focus();
  if (typeof el.select === 'function' && el.tagName !== 'SELECT') { try { el.select(); } catch (e) {} }
  return true;
})()
"""


class _PageParser(HTMLParser):
    """Collect visible text and the interactive surface of a document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.elements: list[dict[str, Any]] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._stack: list[dict[str, Any]] = []
        self._hidden_depth = 0
        # Tags that opened a hidden region, so the region closes where it ends
        # instead of hiding the rest of the document.
        self._hidden_open: list[str] = []

    # -- text -----------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        inline_hidden = _INLINE_HIDDEN.search(attributes.get("style", "")) is not None
        if tag in _SKIP_TEXT:
            self._skip_depth += 1
        if (
            inline_hidden
            or "hidden" in attributes
            or attributes.get("aria-hidden", "").lower() == "true"
        ):
            self._hidden_depth += 1
            self._hidden_open.append(tag)
        if tag == "title":
            self._in_title = True
        role = (
            None
            if self._hidden_depth or self._skip_depth
            else self._role_for(tag, attributes)
        )
        if role is not None:
            entry = {
                "ref": f"e{len(self.elements) + 1}",
                "role": role,
                "tag": tag,
                "name": self._name_for(tag, attributes),
                "value": collapse(attributes.get("value", "")),
                "href": attributes.get("href", "") if tag == "a" else "",
                "type": attributes.get("type", "") if tag in {"input", "button"} else "",
                "disabled": "disabled" in attributes,
                "id": attributes.get("id", ""),
                "name_attr": attributes.get("name", ""),
                "text": [] if tag not in _VOID else None,
            }
            self.elements.append(entry)
            if tag not in _VOID and tag != "option":
                self._stack.append(entry)
        elif tag not in _VOID:
            self._stack.append({"ref": "", "text": []})

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TEXT and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if self._hidden_open and self._hidden_open[-1] == tag:
            self._hidden_open.pop()
            self._hidden_depth = max(0, self._hidden_depth - 1)
        if tag in _VOID:
            return
        if self._stack:
            entry = self._stack.pop()
            collected = entry.get("text")
            if collected and entry.get("ref"):
                entry["name"] = collapse(" ".join(collected)) or entry.get("name", "")

    def handle_data(self, data: str) -> None:
        # The title lives inside <head>, which is skipped for body text, so it is
        # read before either the skip or the hidden checks.
        if self._in_title:
            self.title = self.title or collapse(data, 200)
            return
        if self._skip_depth or self._hidden_depth:
            return
        text = collapse(data, 400)
        if not text:
            return
        self.text_parts.append(data)
        for entry in self._stack:
            if entry.get("text") is not None and entry.get("ref"):
                entry["text"].append(text)

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _role_for(tag: str, attributes: dict[str, str]) -> str | None:
        return role_for(tag, attributes.get("type", ""), attributes.get("role", ""))

    @staticmethod
    def _name_for(tag: str, attributes: dict[str, str]) -> str:
        for key in ("aria-label", "placeholder", "title", "alt", "value", "name"):
            value = attributes.get(key, "")
            if value:
                return collapse(value)
        return ""


def role_for(tag: str, type_name: str = "", role_attr: str = "") -> str | None:
    """Accessible role for an element, shared by both observation sources."""
    explicit = (role_attr or "").strip().lower()
    if explicit:
        return explicit
    tag = (tag or "").lower()
    if tag == "input":
        return _INPUT_ROLES.get((type_name or "text").lower(), "textbox")
    if tag in _INTERACTIVE:
        return _INTERACTIVE[tag]
    return None


def parse_html(html: str, *, url: str = "", max_elements: int = MAX_ELEMENTS) -> dict[str, Any]:
    """Turn a document into the canonical observation shape."""
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError):
        # A malformed document still yields whatever was parsed before the fault.
        pass
    text = collapse(" ".join(parser.text_parts), MAX_TEXT_CHARS)
    elements = []
    for entry in parser.elements[:max_elements]:
        elements.append(
            {
                "ref": entry["ref"],
                "role": entry["role"],
                "name": entry["name"],
                "tag": entry["tag"],
                "type": entry["type"],
                "value": entry["value"],
                "href": entry["href"],
                "disabled": entry["disabled"],
                "selector_hint": (
                    f"#{entry['id']}" if entry["id"]
                    else (f"[name='{entry['name_attr']}']" if entry["name_attr"] else "")
                ),
            }
        )
    observation = {
        "source": "http",
        "url": url,
        "title": parser.title,
        "text": text,
        "elements": elements,
        "elements_total": len(parser.elements),
        "truncated": len(parser.elements) > len(elements),
        "text_truncated": len(text) >= MAX_TEXT_CHARS,
        "coordinates": False,
    }
    observation["digest"] = digest(observation)
    return observation


def digest(observation: dict[str, Any]) -> str:
    """Fingerprint of what a worker can observe.

    Two observations with the same digest are the same state for decision
    purposes: same url, same title, same visible text, same actionable elements.
    Volatile numbers (element counts, truncation flags) are excluded on purpose,
    so a digest change always means the surface really changed.
    """
    signature = {
        "url": observation.get("url", ""),
        "title": observation.get("title", ""),
        "text": observation.get("text", ""),
        "refs": [
            [
                item.get("ref"),
                item.get("role"),
                item.get("name"),
                item.get("value", ""),
                item.get("href", ""),
                bool(item.get("disabled")),
            ]
            for item in observation.get("elements", [])
            if isinstance(item, dict)
        ],
    }
    blob = json.dumps(signature, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def summarise(observation: dict[str, Any] | None, *, max_elements: int = 40) -> str:
    """Short, prompt-sized rendering of an observation."""
    if not isinstance(observation, dict) or not observation:
        return ""
    lines = [
        f"source={observation.get('source')} url={observation.get('url')}",
        f"title={observation.get('title')}",
        f"digest={observation.get('digest')}",
    ]
    text = str(observation.get("text", ""))
    if text:
        lines.append("text=" + text[:2_000])
    elements = [item for item in observation.get("elements", []) if isinstance(item, dict)]
    if elements:
        lines.append("elements:")
        for item in elements[:max_elements]:
            rect = item.get("rect")
            where = (
                f" at ({int(rect['x'])},{int(rect['y'])}) {int(rect['width'])}x{int(rect['height'])}"
                if isinstance(rect, dict)
                else ""
            )
            value = f" value={item['value']!r}" if item.get("value") else ""
            lines.append(
                f"  {item.get('ref')} {item.get('role')} {item.get('name')!r}"
                f"{value} tag={item.get('tag')}{where}"
            )
        if observation.get("elements_total", 0) > len(elements[:max_elements]):
            lines.append(f"  ... {observation['elements_total']} elements in total")
    return "\n".join(lines)


def find(observation: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Return the element with this reference, if the observation has it."""
    for item in observation.get("elements", []):
        if isinstance(item, dict) and item.get("ref") == ref:
            return item
    return None


class PageError(RuntimeError):
    """No observable surface could be reached."""


def _normalise_live(raw: Any, *, max_elements: int = MAX_ELEMENTS) -> dict[str, Any]:
    """Shape what the page script returned into the canonical observation."""
    if not isinstance(raw, dict):
        raise PageError("the page returned no observation")
    elements = []
    for item in list(raw.get("elements") or [])[:max_elements]:
        if not isinstance(item, dict):
            continue
        elements.append(
            {
                "ref": item.get("ref"),
                "role": role_for(
                    str(item.get("tag", "")),
                    str(item.get("type", "")),
                    str(item.get("role_attr", "")),
                )
                or "generic",
                "name": collapse(str(item.get("name", ""))),
                "tag": item.get("tag"),
                "type": item.get("type", ""),
                "value": collapse(str(item.get("value", ""))),
                "href": item.get("href", ""),
                "disabled": bool(item.get("disabled")),
                "rect": item.get("rect"),
            }
        )
    observation = {
        "source": "cdp",
        "url": raw.get("url", ""),
        "title": collapse(str(raw.get("title", "")), 200),
        "text": collapse(str(raw.get("text", "")), MAX_TEXT_CHARS),
        "elements": elements,
        "elements_total": int(raw.get("elements_total") or len(elements)),
        "truncated": int(raw.get("elements_total") or len(elements)) > len(elements),
        "coordinates": True,
        "viewport": raw.get("viewport"),
    }
    observation["digest"] = digest(observation)
    return observation


async def observe(
    *,
    url: str | None = None,
    port: int | None = None,
    timeout: float = 10.0,
    allow_fetch: bool = True,
) -> dict[str, Any]:
    """Observe the surface: live DOM when a devtools port answers, HTML otherwise.

    The live path is preferred because it sees the page *as the user sees it*
    after JavaScript ran, and because it reports element boxes, which is what
    makes a click by reference possible instead of guessing coordinates.
    """
    from . import cdp

    if cdp.available():
        ports = (port,) if port else cdp.DEFAULT_PORTS
        found = await cdp.find_page(tuple(ports), url_contains=url)
        if found is not None:
            _found_port, target = found
            try:
                async with cdp.CdpSession(
                    str(target["webSocketDebuggerUrl"]), timeout=timeout
                ) as session:
                    raw = await session.evaluate(OBSERVATION_SCRIPT)
                return _normalise_live(raw)
            except cdp.CdpError as error:
                if not allow_fetch:
                    raise PageError(str(error)) from error
    if not url or not allow_fetch:
        raise PageError(
            "no debuggable browser is listening and no url was given to fetch"
        )
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=min(max(timeout, 1.0), 30.0), follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            html = response.text
    except (httpx.HTTPError, OSError) as error:
        raise PageError(f"cannot fetch {url}: {error}") from error
    observation = parse_html(html, url=str(response.url))
    observation["status_code"] = response.status_code
    return observation


def matches(observation: dict[str, Any], predicate: dict[str, Any]) -> bool:
    """Evaluate a bounded, deterministic predicate against an observation.

    Supported keys, all optional and AND-ed:

    - ``text_contains``: substring of the visible text (case-insensitive);
    - ``title_contains``: substring of the title;
    - ``url_contains``: substring of the url;
    - ``element_present``: a ``{role, name}`` pair that must exist;
    - ``digest_changed``: true when the observation differs from
      ``previous_digest``, which is what a "the click worked" check needs.
    """
    if not isinstance(predicate, dict):
        return False
    text = str(observation.get("text", "")).casefold()
    if "text_contains" in predicate:
        if str(predicate["text_contains"]).casefold() not in text:
            return False
    if "title_contains" in predicate:
        if str(predicate["title_contains"]).casefold() not in str(
            observation.get("title", "")
        ).casefold():
            return False
    if "url_contains" in predicate:
        if str(predicate["url_contains"]).casefold() not in str(
            observation.get("url", "")
        ).casefold():
            return False
    if "element_present" in predicate:
        wanted = predicate["element_present"] or {}
        role = str(wanted.get("role", "")).casefold()
        name = str(wanted.get("name", "")).casefold()
        found = False
        for item in observation.get("elements", []):
            if not isinstance(item, dict):
                continue
            if role and str(item.get("role", "")).casefold() != role:
                continue
            if name and name not in str(item.get("name", "")).casefold():
                continue
            found = True
            break
        if not found:
            return False
    if predicate.get("digest_changed"):
        previous = predicate.get("previous_digest")
        if previous and observation.get("digest") == previous:
            return False
    return True
