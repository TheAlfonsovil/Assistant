"""Is UI Automation usable here, and how much does a window snapshot cost?

UIA is the Windows accessibility tree: the same idea as ``browser.snapshot`` but
for native windows. This probe answers three questions with measurements: does it
run on this host, how long does it take, and how big is the result for a real
window.
"""

from __future__ import annotations

import json
import sys
import time

try:
    import uiautomation as auto
except ImportError as error:  # pragma: no cover - probe only
    print("uiautomation NOT available:", error)
    raise SystemExit(3)

started = time.perf_counter()
root = auto.GetRootControl()
windows = root.GetChildren()
elapsed = time.perf_counter() - started
print(f"ventanas de nivel superior: {len(windows)} (en {elapsed:.2f}s)")
for window in windows[:8]:
    try:
        print(
            f"  - {window.Name[:60]!r} | clase={window.ClassName} | pid={window.ProcessId} "
            f"| rect={window.BoundingRectangle}"
        )
    except Exception as error:  # noqa: BLE001
        print("  - (error)", error)

foreground = auto.GetForegroundControl()
print("ventana en primer plano:", repr(foreground.Name[:60]), foreground.ClassName)

started = time.perf_counter()
count = 0
samples = []
walker = auto.WalkControl(foreground, maxDepth=4)
for item in walker:
    control = item[0] if isinstance(item, tuple) else item
    count += 1
    if len(samples) < 12:
        samples.append(
            {
                "type": control.ControlTypeName,
                "name": (control.Name or "")[:40],
                "enabled": control.IsEnabled,
            }
        )
elapsed = time.perf_counter() - started
payload = json.dumps(samples, ensure_ascii=False)
print(f"controles recorridos: {count} (profundidad 4) en {elapsed:.2f}s")
print("muestra:", payload[:400])
print("coste estimado del arbol (12 controles):", len(payload), "chars")
