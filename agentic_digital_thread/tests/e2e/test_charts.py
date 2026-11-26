# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
End-to-end check that Chart.js renders inside the sandboxed chart iframe.

Chat/thread answers may contain a ``<chart-block>`` that chat.js turns into a
``sandbox="allow-scripts"`` iframe whose ``srcdoc`` loads Chart.js from the
*absolute* same-origin URL ``${window.location.origin}/js/vendor/chart.umd.min.js``.
The absolute URL is deliberate: a sandboxed iframe without ``allow-same-origin``
has an opaque origin, so a root-relative ``/js/vendor/...`` path would not
resolve. This test reproduces that exact mechanism so we get deterministic
coverage without needing an onboarded system that emits a chart-block.

The probe iframe ``postMessage``s its result back to the parent (the only
channel available across the opaque sandbox boundary), reporting whether the
``Chart`` global loaded and whether a chart instance actually drew to canvas.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

# Builds a sandboxed allow-scripts iframe identical in shape to the one chat.js
# injects, loads Chart.js from the absolute same-origin URL, instantiates a
# chart, and reports the outcome to the parent via postMessage.
_CHART_PROBE_JS = r"""
async () => {
  const origin = window.location.origin;
  return await new Promise((resolve) => {
    let settled = false;
    const finish = (r) => { if (!settled) { settled = true; resolve(r); } };

    const onMessage = (event) => {
      if (event.data && event.data.__chartProbe) {
        window.removeEventListener('message', onMessage);
        finish(event.data);
      }
    };
    window.addEventListener('message', onMessage);

    const chartConfig = {
      type: 'bar',
      data: { labels: ['A', 'B', 'C'], datasets: [{ data: [3, 1, 2] }] },
      options: { responsive: false, animation: false },
    };

    const chartUrl = origin + '/js/vendor/chart.umd.min.js';
    const html =
      '<!DOCTYPE html><html><head>' +
      '<script src="' + chartUrl + '" ' +
      'onerror="parent.postMessage({__chartProbe:true,loaded:false,drew:false,error:\'script load failed\'},\'*\')">' +
      '<\/script></head><body><canvas id="c" width="240" height="140"></canvas><script>' +
      'try {' +
      '  var ok = (typeof Chart !== "undefined");' +
      '  var drew = false;' +
      '  if (ok) {' +
      '    var ch = new Chart(document.getElementById("c"), ' + JSON.stringify(chartConfig) + ');' +
      '    drew = !!(ch && ch.canvas && ch.canvas.width > 0);' +
      '  }' +
      '  parent.postMessage({__chartProbe:true, loaded: ok, drew: drew}, "*");' +
      '} catch (err) {' +
      '  parent.postMessage({__chartProbe:true, loaded:false, drew:false, error: String(err)}, "*");' +
      '}' +
      '<\/script></body></html>';

    const iframe = document.createElement('iframe');
    iframe.setAttribute('sandbox', 'allow-scripts');
    iframe.style.cssText = 'width:240px;height:140px;border:0;';
    iframe.srcdoc = html;
    document.body.appendChild(iframe);

    setTimeout(() => finish({__chartProbe: true, loaded: false, drew: false, error: 'timeout'}), 15000);
  });
}
"""


@pytest.mark.e2e
def test_chartjs_renders_in_sandboxed_iframe(authed_page: Page) -> None:
    """Chart.js loads from the same-origin vendor URL and renders inside the
    opaque-origin sandboxed iframe — the same path used by chat/thread charts."""
    result = authed_page.evaluate(_CHART_PROBE_JS)
    assert result.get("loaded") is True, (
        "Chart.js did not load from same-origin /js/vendor/chart.umd.min.js "
        f"inside the sandboxed iframe: {result}"
    )
    assert result.get("drew") is True, (
        f"Chart.js loaded but the chart did not render to canvas: {result}"
    )
