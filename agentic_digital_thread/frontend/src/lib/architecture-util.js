// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/* Agentic Digital Thread — Architecture map helpers.
 *
 * Pure, DOM-free logic behind the System-Map / Investigation-graph flow
 * animation. Kept in its own module so it can be unit-tested under node (see
 * tests/unit/test_architecture_util.py) as well as executed as a side-effect
 * import publishing browser globals.
 */
(function (global) {
  'use strict';

  /**
   * Map a tool name to the system id it queries.
   *
   * Handles three shapes, in order:
   *   1. Knowledge-graph exploration ("explore_graph") is the hub node, not a
   *      leaf system, so it returns null (callers route it to the neptune node).
   *   2. Gateway tool names, formatted "<system_id>___<tool>" (e.g.
   *      "lake-athena___athena_run_query"), return the "<system_id>" prefix.
   *   3. Legacy fallback: "query_<system_id_with_underscores>" (e.g.
   *      "query_mes_mil") returns the dash-cased system id ("mes-mil").
   * Anything else (e.g. "get_system_schema", empty/non-string) returns null.
   *
   * Pure: no DOM, no globals, no side effects.
   *
   * @param {string} toolName raw tool name from a tool_start/tool_result event
   * @returns {?string} the system id, or null when the tool is not a system leaf
   */
  function mapToolToSystemId(toolName) {
    if (!toolName || typeof toolName !== 'string') return null;
    // Knowledge-graph exploration is the middle hub node, not a leaf system.
    if (toolName === 'explore_graph') return null;
    // Gateway tool format: "<system_id>___<tool>" → take the system_id prefix.
    if (toolName.indexOf('___') !== -1) {
      var sysId = toolName.split('___')[0];
      return sysId || null;
    }
    // Legacy fallback: "query_<system_id>" with underscores → dash-cased id.
    if (toolName.indexOf('query_') === 0) {
      return toolName.replace(/^query_/, '').replace(/_/g, '-');
    }
    return null;
  }

  global.mapToolToSystemId = mapToolToSystemId;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { mapToolToSystemId: mapToolToSystemId };
  }
  // ─────────────────────────────────────────────────────────────────────────
  // Investigation-graph natural-language helpers (pure, DOM-free).
  //
  // These turn a tool call's REQUEST (tool_start.input) and RESULT (tool_result
  // .summary) into short plain-English descriptions for the reasoning-trace
  // graph — no raw Cypher/SQL, no extra LLM calls, generic across systems (no
  // hardcoded plant/system names). Appended alongside mapToolToSystemId so the
  // whole investigation-graph presentation layer is node-unit-testable.
  // ─────────────────────────────────────────────────────────────────────────

  // The tool "verb" half of a gateway "<system_id>___<tool>" name (or the raw
  // name when there is no gateway prefix).
  function _toolHalf(tool) {
    if (!tool || typeof tool !== 'string') return tool || '';
    return tool.indexOf('___') !== -1 ? tool.split('___')[1] : tool;
  }

  // Collapse whitespace and hard-truncate with an ellipsis.
  function _truncate(s, n) {
    s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
    n = n || 120;
    return s.length > n ? s.slice(0, n).trimEnd() + '…' : s;
  }

  function _mappingEndpoint(input, side) {
    var system = input[side + '_system'];
    var field = input[side + '_field'];
    var value = input[side + '_value'];
    var endpoint = [system, field].filter(Boolean).join('.');
    if (value != null && String(value).trim()) endpoint += ' “' + _truncate(value, 32) + '”';
    return endpoint || '?';
  }

  function _resultObject(payload) {
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) return payload;
    if (typeof payload !== 'string' || !payload.trim()) return null;
    try {
      var parsed = JSON.parse(payload);
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
    } catch (_err) {
      return null;
    }
  }

  function _shortId(value) {
    var id = String(value == null ? '' : value).trim();
    return id.length > 12 ? id.slice(0, 8) + '…' : id;
  }

  // Pick the most meaningful input field (mirrors formatToolInput's
  // priority list): the NL sub-question in Mode A, the SQL/Cypher in Mode B, …
  function _pickInputValue(input) {
    if (input == null) return null;
    if (typeof input !== 'object') return input;
    if (Array.isArray(input)) return input.length ? JSON.stringify(input) : null;
    var PRIORITY = ['question', 'sub_question', 'subquestion', 'query', 'sql',
      'statement', 'filter', 'expression', 'cypher', 'gremlin', 'prompt', 'text', 'input'];
    var lowerMap = {};
    for (var k in input) {
      if (Object.prototype.hasOwnProperty.call(input, k)) lowerMap[k.toLowerCase()] = k;
    }
    for (var i = 0; i < PRIORITY.length; i++) {
      var actual = lowerMap[PRIORITY[i]];
      if (actual != null && input[actual] != null && String(input[actual]).trim()) return input[actual];
    }
    for (var k2 in input) {
      if (typeof input[k2] === 'string' && input[k2].trim()) return input[k2];
    }
    var scalars = {};
    for (var k3 in input) {
      var v = input[k3];
      if (v == null || typeof v !== 'object') scalars[k3] = v;
    }
    return Object.keys(scalars).length ? JSON.stringify(scalars) : null;
  }

  // Join label names as a readable list: ["System"] → "System"; ["A","B"] →
  // "A and B"; ["A","B","C"] → "A, B and C".
  function _joinList(items) {
    items = (items || []).slice(0, 3);
    if (items.length <= 1) return items[0] || '';
    return items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
  }

  // Heuristically translate a Cypher query into plain English. Generic: node
  // labels and any name filter come straight from the query text, so there are
  // no hardcoded plant/system names.
  function _englishizeCypher(cypher) {
    var s = String(cypher == null ? '' : cypher);
    if (!s.trim()) return '';
    var labels = [];
    // Node labels only — "(var:Label)" / "(:Label)". Relationship types inside
    // "[:REL]" are deliberately excluded.
    var re = /\(\s*[A-Za-z0-9_]*\s*:\s*([A-Za-z_][A-Za-z0-9_]*)/g;
    var m;
    while ((m = re.exec(s)) !== null) {
      if (labels.indexOf(m[1]) === -1) labels.push(m[1]);
    }
    var nameM = s.match(/(?:name|id|label)\s*(?:=|:|CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s*['"]([^'"]{1,48})['"]/i)
      || s.match(/CONTAINS\s+['"]([^'"]{1,48})['"]/i)
      || s.match(/=\s*['"]([^'"]{1,48})['"]/);
    var phrase;
    if (labels.length) {
      phrase = 'Looked up ' + _joinList(labels) + ' node' + (labels.length === 1 ? '' : 's') + ' in the knowledge graph';
    } else {
      phrase = 'Explored the knowledge graph';
    }
    if (nameM) phrase += ' matching “' + _truncate(nameM[1], 40) + '”';
    return phrase;
  }

  // Whether the text holds a markdown table with a row under its header: a
  // "|---|" separator line and at least two other pipe rows. Every pipe row
  // in the text is read, in all of its tables, so this says only that a table
  // is there, never how many rows it has.
  function _holdsTableRows(s) {
    var lines = String(s || '').split('\n');
    var pipeRows = 0;
    var hasSep = false;
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].trim();
      if (ln.indexOf('|') === -1 || ln.split('|').length < 3) continue;
      if (/^\|?[\s:\-|]+\|?$/.test(ln) && ln.indexOf('-') !== -1) { hasSep = true; continue; }
      pipeRows++;
    }
    return hasSep && pipeRows > 1; // the first pipe row is the header
  }

  /**
   * Describe a tool call's REQUEST in plain English (the "last request" sub-line
   * under a system node). Cypher is heuristically Englishized; a Mode-A NL
   * sub-question is passed through smart-truncated; save_* writes are phrased.
   * @param {string} tool  raw tool name
   * @param {*}      input tool_start.input (object or string)
   * @returns {string}
   */
  function describeToolInput(tool, input) {
    var half = _toolHalf(tool);
    if (input && typeof input === 'object' && !Array.isArray(input)) {
      if (half === 'save_value_mapping' || (input.from_system && input.to_system)) {
        var md = 'Saving learned mapping: ' + _mappingEndpoint(input, 'from') + ' → ' + _mappingEndpoint(input, 'to');
        if (input.concept) md += ' (' + input.concept + ')';
        return md;
      }
      if (half === 'save_correlation' || input.pattern) {
        var systems = Array.isArray(input.systems) ? input.systems.filter(Boolean) : [];
        var cd = 'Saving learned correlation';
        if (systems.length) cd += ': ' + systems.join(' → ');
        if (input.pattern) cd += ' — “' + _truncate(input.pattern, 72) + '”';
        return cd;
      }
      if (half && half.indexOf('save_') === 0) return 'Saving learned knowledge';
    }
    var val = _pickInputValue(input);
    if (val == null || !String(val).trim()) return '';
    var sval = String(val);
    // System-agent requests (query_* tools other than explore_graph) are
    // English by contract — never treat them as Cypher (a bare "return" in a
    // natural-language request must not read as knowledge-graph exploration).
    var isSystemQuery = half !== 'explore_graph' && half && half.indexOf('query_') === 0;
    var isCypher = !isSystemQuery
      && (half === 'explore_graph'
        || (input && typeof input === 'object' && input.cypher)
        || /\bMATCH\s*\(/i.test(sval));
    if (isCypher) return _englishizeCypher(sval);
    return _truncate(sval, 120);
  }

  /** A short summary that says nothing was found ("No matching data"). */
  function _saysNothingFound(s) {
    return /\bno\b[^.\n]*\b(found|match|matching|results|data|records|rows|nodes|entities)\b/i.test(s)
      && s.length < 80;
  }

  // Markdown that is structure only and never the result line: a heading, a
  // rule or setext underline, a fence (and the lines inside it), and a bold
  // label line ("**Result:**", "**Summary**"). A bold line that holds the
  // result itself ("**42 orders shipped late.**") is content.
  var _HEADING_LINE_RE = /^#{1,6}(?:\s|$)/;
  // A rule or setext underline: at least three of - = * _, with whitespace
  // allowed after each, and nothing else. Two flat checks (the characters,
  // then the marker count) instead of one pattern with a repeated group.
  var _RULE_CHARS_RE = /^[-=*_][-=*_\s]*$/;
  function _isRuleLine(ln) {
    return _RULE_CHARS_RE.test(ln) && ln.replace(/[^-=*_]/g, '').length >= 3;
  }
  var _FENCE_LINE_RE = /^(?:```|~~~)/;
  var _BOLD_LINE_RE = /^(?:\*\*([^*\n]+)\*\*|__([^_\n]+)__)(:?)$/;

  // A line that is one bold span used as a section label: it ends in a colon
  // ("**Result:**"), or its words are a known section label ("**Summary**").
  // Any other bold line is the result itself ("**Yes**", "**Failed**").
  var _SECTION_LABELS = ['result', 'results', 'summary', 'answer', 'findings'];
  function _isBoldLabel(ln) {
    var m = _BOLD_LINE_RE.exec(ln);
    if (!m) return false;
    var text = (m[1] || m[2]).trim();
    if (m[3] === ':' || /:$/.test(text)) return true;
    return _SECTION_LABELS.indexOf(text.toLowerCase()) !== -1;
  }

  // A markdown table row or separator (the rows _holdsTableRows reads).
  function _isTableLine(ln) {
    return ln.indexOf('|') !== -1 && ln.split('|').length >= 3;
  }

  // One line's inline markdown as plain text: quote and list markers, links
  // and images (their text kept), inline code (its text kept as written),
  // bold and italic markers. A struck-through span is dropped whole:
  // markdown marks it as no longer true. Code spans are set aside first, so
  // no rule reaches the text inside one. An underscore inside a name
  // (run_number) is kept.
  function _plainInline(ln) {
    return _plainText(ln, _stripMarkers);
  }

  // A table cell's inline markdown as plain text. A cell holds no quote or
  // list marker (String leaves the text as it is), and its escaped pipes
  // (\|) are pipes, as the renderer shows them, inside a code span too.
  function _plainCell(cell) {
    return _plainText(cell.replace(/\\\|/g, '|'), String);
  }

  // Inline markdown as plain text; `markers` removes the leading block
  // markers the text may carry.
  function _plainText(ln, markers) {
    var codes = [];
    var text = markers(ln).replace(/`([^`\n]*)`/g, function (_m, code) {
      codes.push(code);
      return '\u0000' + (codes.length - 1) + '\u0000';
    });
    // A struck span at the start of the line can expose a marker after it.
    text = markers(text.replace(/~~(?=\S)[^\n]*?\S~~/g, ' '));
    // Spaces are normalized before code text is put back, so a code span
    // keeps its own spacing as written.
    return _plainProse(text)
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/\u0000(\d+)\u0000/g, function (m, n) { return n < codes.length ? codes[Number(n)] : m; });
  }

  // Leading quote and list markers.
  function _stripMarkers(s) {
    return s.replace(/^\s*(?:>\s*)+/, '').replace(/^\s*(?:[-*+]|\d{1,3}[.)])\s+/, '');
  }

  // Links and images (text kept), bold and italic markers.
  function _plainProse(part) {
    return part
      .replace(/!?\[([^\]\n]*)\]\([^)\n]*\)/g, '$1')
      .replace(/\*\*(?=\S)([^\n]*?\S)\*\*/g, '$1')
      .replace(/(^|[^\w])__(?=\S)([^\n]*?\S)__(?!\w)/g, '$1$2')
      .replace(/(^|[^\w*])\*(?=\S)([^*\n]*?\S)\*(?![\w*])/g, '$1$2')
      .replace(/(^|[^\w])_(?=\S)([^_\n]*?\S)_(?!\w)/g, '$1$2');
  }

  // A letter or a number in any script: a line with neither is not content.
  var _CONTENT_RE = /[\p{L}\p{N}]/u;

  // One trimmed line outside a fence and a table, as plain text. '' for a
  // heading, a rule or setext underline, a bold section label, or a line
  // left with no letter or number.
  function _plainLine(ln) {
    if (_HEADING_LINE_RE.test(ln) || _isRuleLine(ln) || _isBoldLabel(ln)) return '';
    var plain = _plainInline(ln);
    return _CONTENT_RE.test(plain) ? plain : '';
  }

  // The lines of real content the prose line picks from, as plain text:
  // markdown structure is skipped (a fence and its lines, and any line with
  // two pipes as a table row) and inline syntax removed. A line left with no
  // letter or number is dropped.
  function _contentLines(s) {
    var lines = String(s == null ? '' : s).split('\n');
    var out = [];
    var inFence = false;
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].trim();
      if (_FENCE_LINE_RE.test(ln)) { inFence = !inFence; continue; }
      if (inFence || !ln || _isTableLine(ln)) continue;
      var plain = _plainLine(ln);
      if (plain) out.push(plain);
    }
    return out;
  }

  // Leading spaces, a tab counted as four.
  function _indent(raw) {
    return /^[ \t]*/.exec(raw)[0].replace(/\t/g, '    ').length;
  }

  // A fenced code opener: at most three spaces, then three or more backticks
  // (no backtick after them on the line) or tildes. null when the line is
  // not one.
  function _fenceOpen(raw) {
    var m = /^ {0,3}(`{3,}(?=[^`]*$)|~{3,})/.exec(raw);
    return m ? { ch: m[1][0], n: m[1].length } : null;
  }

  // Whether raw closes the open fence: the same character, at least as many.
  function _closesFence(raw, fence) {
    var m = /^ {0,3}(`{3,}|~{3,})\s*$/.exec(raw);
    return !!m && m[1][0] === fence.ch && m[1].length >= fence.n;
  }

  // A line that is never paragraph text: a heading, a fence, a thematic break.
  var _HEADING_FENCE_OPEN_RE = /^ {0,3}(?:#{1,6}(?:\s|$)|`{3}|~{3})/;
  // A thematic break: up to three spaces, then at least three of one marker
  // (- * _), with spaces or tabs allowed after each, and nothing else.
  var _BREAK_CHARS_RE = /^ {0,3}([-*_])[-*_ \t]*$/;
  function _isThematicBreak(raw) {
    var m = _BREAK_CHARS_RE.exec(raw);
    if (!m) return false;
    var marks = raw.replace(/[ \t]/g, '');
    return marks.length >= 3 && marks.split(m[1]).join('') === '';
  }
  function _opensLeaf(raw) {
    return _HEADING_FENCE_OPEN_RE.test(raw) || _isThematicBreak(raw);
  }
  // A quote or list item marker: the paragraph after it is inside that
  // block, and a following line with no marker continues it.
  var _CONTAINER_OPEN_RE = /^ {0,3}(?:>|[-*+](?:\s|$)|\d{1,9}[.)](?:\s|$))/;

  // A line that opens another block, so it is never a table row: a heading,
  // a fence, a thematic break, a quote, a list item, or indented code.
  function _opensBlock(raw) {
    return _indent(raw) > 3 || _opensLeaf(raw) || _CONTAINER_OPEN_RE.test(raw);
  }

  // A table row's cells by the GFM table grammar: split on pipes that are
  // not escaped (\|), the outer pipes optional.
  function _rowCells(raw) {
    var ln = raw.trim();
    var cells = [''];
    for (var i = 0; i < ln.length; i++) {
      var c = ln[i];
      if (c === '\\' && (ln[i + 1] === '|' || ln[i + 1] === '\\')) {
        cells[cells.length - 1] += c + ln[++i];
      } else if (c === '|') {
        cells.push('');
      } else {
        cells[cells.length - 1] += c;
      }
    }
    if (ln[0] === '|') cells.shift();
    // A lone "|" is both outer pipes, so it holds no cell: never a head row.
    if (cells.length && ln[ln.length - 1] === '|' && cells[cells.length - 1] === '') cells.pop();
    return cells;
  }

  // A delimiter row's cell ("---", ":--", "--:", ":-:").
  var _DELIM_CELL_RE = /^:?-+:?$/;

  // A setext heading underline ("===" or "--"): under a paragraph line it
  // makes that line a heading, so it is never a table's head row.
  var _SETEXT_RE = /^ {0,3}(?:=+|-+)[ \t]*$/;

  // The column count of the table whose head row is raw and whose delimiter
  // row is next, by the GFM table grammar the drill-down renders with: the
  // delimiter row holds only delimiter cells and a pipe or a colon (else it
  // is a rule or an underline), and the head row has as many cells. 0 when
  // the two lines are not a table head.
  function _tableHeadCols(raw, next) {
    if (next == null || !raw.trim() || _opensBlock(raw) || _opensBlock(next)) return 0;
    if (!/[|:]/.test(next)) return 0;
    var delim = _rowCells(next);
    for (var i = 0; i < delim.length; i++) {
      if (!_DELIM_CELL_RE.test(delim[i].trim())) return 0;
    }
    return _rowCells(raw).length === delim.length ? delim.length : 0;
  }

  // A table row as one plain line: its cells' text joined by " · ", at most
  // the table's column count (the renderer shows no more). '' for a row with
  // no text or one of delimiter cells only.
  function _tableRowText(raw, cols) {
    var cells = _rowCells(raw).slice(0, cols).map(function (c) { return _plainCell(c); }).filter(Boolean);
    var delimOnly = cells.every(function (c) { return _DELIM_CELL_RE.test(c); });
    return delimOnly ? '' : cells.join(' · ');
  }

  // The first line of real content, as plain text. '' when the text holds no
  // content.
  function _firstContentLine(s) {
    return _contentLines(s)[0] || '';
  }

  // The prose line: the first sentence of the first line of real content,
  // as plain text (markdown syntax never becomes the line), truncated.
  function _proseLine(s) {
    var firstLine = _firstContentLine(s);
    if (!firstLine) return '';
    var dot = firstLine.indexOf('. ');
    var frag = dot > 20 ? firstLine.slice(0, dot + 1) : firstLine;
    // _firstContentLine already joined spaces outside code; cap the length
    // without _truncate's whitespace collapse so a code span keeps its own.
    return frag.length > 100 ? frag.slice(0, 100).trimEnd() + '…' : frag;
  }

  // The line a branch ahead of the prose line gives a summary that is not a
  // save: a failed graph query, "nothing found", the server's records-found
  // count (built from the result rows), a knowledge-graph log. '' when none
  // applies. A graph query's lines are never counted: the server shows at
  // most 8 of them, a row can be a scalar, and the routing step is prose, so
  // nothing typed says how many entities came back.
  function _fixedLine(half, s) {
    // A failed graph query (the server's "Error: Graph query failed …"
    // summary) is a failure: never "No matching data".
    if (half === 'explore_graph' && /^(?:Error:|Query error\b)/i.test(s)) return 'Graph query failed';
    // Explicit "nothing found".
    if (_saysNothingFound(s)) return 'No matching data';
    var recM = s.match(/^(\d+)\s+records?\s+found/i);
    if (recM) return 'Returned ' + recM[1] + ' record' + (recM[1] === '1' ? '' : 's');
    if (/^Result with \d+ fields?/i.test(s) || /"?status"?\s*:\s*"?logged/i.test(s)) {
      return 'Logged to the knowledge graph';
    }
    return '';
  }

  /**
   * Whether describeToolResult gives this summary its prose line although the
   * summary holds a markdown table: the same branches, in the same order, with
   * no text built. A reply's table is its own layout, so its rows are never
   * read as a count of what the call returned. A caller that holds a typed
   * count for the call (the telemetry reads line) shows it in place of that
   * prose line.
   * @param {string} tool     raw tool name
   * @param {string} summary  tool_result.summary
   * @returns {boolean}
   */
  function readsPastTable(tool, summary) {
    var s = String(summary == null ? '' : summary).trim();
    if (!s) return false;
    var half = _toolHalf(tool);
    if (half && half.indexOf('save_') === 0) return false;
    return !_fixedLine(half, s) && _holdsTableRows(s);
  }

  /**
   * The text as plain text, one line per line of content, for a surface that
   * shows more of a summary than its prose line: a graph node's hover
   * tooltip, or a label that joins several lines into one (no list marker is
   * left mid-line). It keeps what the prose line skips, so a result that is
   * only a table or only code still reads:
   * - a fenced code line is kept as written;
   * - a table row, by the GFM table grammar the drill-down renders with (a
   *   head row, a delimiter row with as many cells, then body rows up to a
   *   blank line or another block), becomes its cells' text joined by " · ",
   *   and only the delimiter row is left out; a pipe anywhere else is text.
   * A heading, a rule and a bold section label are left out, inline syntax
   * is removed, and a line with no letter or number in any script is
   * dropped. '' when the text holds no content.
   * @param {string} text
   * @returns {string}
   */
  function plainSummaryText(text) {
    var lines = String(text == null ? '' : text).split('\n');
    var out = [];
    var fence = null; // the open code fence
    var cols = 0; // the column count of the table the next line may continue
    // The line before is paragraph text ("===" under it is a heading
    // underline), and that paragraph is in a quote or list item (a line under
    // it with no marker continues it, so it starts no table).
    var para = false;
    var lazy = false;
    for (var i = 0; i < lines.length; i++) {
      var raw = lines[i];
      var line = '';
      var wasPara = para;
      var wasLazy = lazy;
      para = lazy = false;
      if (fence) {
        if (_closesFence(raw, fence)) fence = null;
        else line = raw.trim();
      } else if (cols && raw.trim() && !_opensBlock(raw)) {
        line = _tableRowText(raw, cols);
      } else {
        var underline = wasPara && !wasLazy && _SETEXT_RE.test(raw);
        cols = underline || wasLazy ? 0 : _tableHeadCols(raw, lines[i + 1]);
        if (cols) {
          line = _tableRowText(raw, cols);
          i++;
        } else {
          fence = _fenceOpen(raw);
          if (!fence) line = _plainLine(raw.trim());
        }
        para = !cols && !fence && !underline && Boolean(raw.trim()) && !_opensLeaf(raw);
        lazy = para && (wasLazy || _CONTAINER_OPEN_RE.test(raw));
      }
      if (line) out.push(line);
    }
    return out.join('\n');
  }

  /**
   * Describe the DATA a tool call returned in plain English (the green result
   * box) — deterministic heuristics only, no LLM. Phrases knowledge-graph
   * writes, detects "nothing found" and a failed graph query, and repeats the
   * server's records-found count. Otherwise the line is the first sentence of
   * the summary's first line of real content, as plain text: markdown syntax
   * never becomes the line, and neither a reply's table rows nor a graph
   * result's lines become a count.
   * @param {string} tool        raw tool name
   * @param {string} summary     tool_result.summary (markdown/prose/one-liner)
   * @param {string} [requestText] not read: the line takes no word from the
   *   request (kept so resultPayload stays the fourth argument)
   * @param {*} [resultPayload] raw tool_result payload, when available
   * @returns {string}
   */
  function describeToolResult(tool, summary, requestText, resultPayload) {
    var s = String(summary == null ? '' : summary).trim();
    if (!s) return '';
    var half = _toolHalf(tool);
    if (half && half.indexOf('save_') === 0) {
      var result = _resultObject(resultPayload);
      var status = result && String(result.status || '').toLowerCase();
      var reason = result && result.reason ? ': ' + _truncate(result.reason, 100) : '';
      if (status === 'dropped' || status === 'filtered' || status === 'skipped') {
        return 'Save filtered by learning gates' + reason;
      }
      if (status === 'rejected' || status === 'failed' || status === 'error') {
        return 'Save rejected' + reason;
      }
      if (status === 'logged' || status === 'saved' || status === 'ok' || status === 'success') {
        var kind = half === 'save_correlation' || String(result.type || '').toLowerCase() === 'insight'
          ? 'correlation' : 'mapping';
        var mappingId = _shortId(result.mapping_id);
        return 'Saved ' + kind + (mappingId ? ' ' + mappingId : '');
      }
      if (/^Saved\b/i.test(s)) return _truncate(s.split('\n')[0], 100);
      if (/^Result with \d+ fields?/i.test(s)) {
        return half === 'save_correlation' ? 'Correlation save completed' : 'Mapping save completed';
      }
      return _truncate(s.split('\n')[0], 100);
    }
    // A fixed line when one applies, else the prose line: the first sentence
    // of the first line of real content.
    return _fixedLine(half, s) || _proseLine(s);
  }

  global.describeToolInput = describeToolInput;
  global.describeToolResult = describeToolResult;
  global.readsPastTable = readsPastTable;
  global.plainSummaryText = plainSummaryText;

  // Augment the module export additively — do NOT reassign the object literal
  // above, so this stays merge-safe alongside the mapToolToSystemId export.
  if (typeof module !== 'undefined' && module.exports) {
    module.exports.describeToolInput = describeToolInput;
    module.exports.describeToolResult = describeToolResult;
    module.exports.readsPastTable = readsPastTable;
    module.exports.plainSummaryText = plainSummaryText;
    // Line classifiers, exported for the tests that pin them to the patterns they replaced.
    module.exports._isRuleLine = _isRuleLine;
    module.exports._opensLeaf = _opensLeaf;
  }
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
