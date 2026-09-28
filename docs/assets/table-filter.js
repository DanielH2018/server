// A filter bar and sortable headers for the long tables on this site -- the generated
// reference pages above all, where Scripts runs past 200 rows across four tables.
//
// ONE BAR PER GROUP, NOT PER TABLE. Tables on one page that share a header row are one group.
// Every multi-table reference page splits a single list by one attribute: Scripts by how each
// script runs, Services by host, Secrets by rotation tier, Decisions by tree area. The heading
// above each table is that attribute's value, so the bar offers it as a "Section" choice, and
// one filter narrows every table in the group at once. A section left with no matching rows is
// hidden while the filter is set, heading and all.
//
// WHICH GROUPS GET A BAR. At least MIN_ROWS body rows and MIN_COLUMNS columns. The column
// floor keeps it off hosts.md's `Fact | Value` tables and the short fragment tables spliced
// into hand-written pages, where a bar would be noise.
//
// WHICH COLUMNS BECOME A DROPDOWN. One whose values repeat -- at most MAX_CHOICES distinct
// values, no more than half the row count, none longer than MAX_CHOICE_LENGTH -- and are not
// all numbers. That picks Directory on Scripts, Platform on Services and Host on Networking.
// Identifier and prose columns are left to the text box, and number columns to sorting.
//
// THE QUERY STRING CARRIES THE FILTER, so a filtered view can be linked and survives a reload.
// Not the hash: that belongs to the row anchors _mkdocs_repo_links.py injects. Other query
// parameters are kept, including the `?h=` Material's search adds. A link into a row the
// filter hides clears the filter, or the link would land on nothing.
//
// Everything above `setUpGroup` is pure, and exported under node for
// scripts/tests/test_table_filter_js.py.

(function () {
  var MIN_ROWS = 10;
  var MIN_COLUMNS = 3;
  var MAX_CHOICES = 25;
  var MAX_CHOICE_LENGTH = 40;
  var SECTION = "Section";
  var TEXT_PARAM = "filter";
  var HIDDEN_ATTR = "data-table-filter-hidden";

  function qualifies(columnCount, rowCount) {
    return columnCount >= MIN_COLUMNS && rowCount >= MIN_ROWS;
  }

  // Numeric-aware, so "Days left" puts 9 before 10 and a date column sorts by date.
  function compareText(a, b) {
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }

  // The columns that get a dropdown, each with its distinct values and how many rows carry
  // each one. `sectionColumn` is exempt from the thresholds, because the Scripts page's
  // headings run longer than MAX_CHOICE_LENGTH, and keeps the order the page gives its
  // sections. Every other column's values are sorted.
  function choiceColumns(headers, rows, sectionColumn) {
    var result = [];
    for (var c = 0; c < headers.length; c++) {
      var forced = c === sectionColumn;
      var counts = new Map();
      for (var r = 0; r < rows.length; r++) {
        var value = rows[r][c];
        counts.set(value, (counts.get(value) || 0) + 1);
      }
      var values = Array.from(counts.keys());
      if (values.length < 2) {
        continue;
      }
      if (!forced) {
        var repeats = values.length <= MAX_CHOICES && values.length * 2 <= rows.length;
        var short = values.every(function (v) {
          return v.length <= MAX_CHOICE_LENGTH;
        });
        var numeric = values.every(function (v) {
          return /^-?\d+(\.\d+)?$/.test(v);
        });
        if (!repeats || !short || numeric) {
          continue;
        }
        values.sort(compareText);
      }
      result.push({
        column: c,
        name: headers[c],
        values: values.map(function (v) {
          return [v, counts.get(v)];
        }),
      });
    }
    return result;
  }

  // A filter is {text, choices}: free text, and a map from column name to the one value that
  // column must hold. Every whitespace-separated word of the text must appear somewhere in
  // the row, in any case.
  function rowMatches(headers, cells, filter) {
    var haystack = cells.join("\n").toLowerCase();
    var words = filter.text.toLowerCase().split(/\s+/);
    for (var w = 0; w < words.length; w++) {
      if (words[w] && haystack.indexOf(words[w]) === -1) {
        return false;
      }
    }
    for (var c = 0; c < headers.length; c++) {
      var name = headers[c];
      if (Object.prototype.hasOwnProperty.call(filter.choices, name)) {
        if (cells[c] !== filter.choices[name]) {
          return false;
        }
      }
    }
    return true;
  }

  function isActive(filter) {
    return filter.text.trim() !== "" || Object.keys(filter.choices).length > 0;
  }

  // `prefix` keeps two groups on one page from reading each other's parameters. The first
  // group has none, so the common case reads as `?Directory=docs`.
  function readFilter(search, names, prefix) {
    var params = new URLSearchParams(search);
    var filter = { text: params.get(prefix + TEXT_PARAM) || "", choices: {} };
    for (var i = 0; i < names.length; i++) {
      if (params.has(prefix + names[i])) {
        filter.choices[names[i]] = params.get(prefix + names[i]);
      }
    }
    return filter;
  }

  function writeFilter(search, names, prefix, filter) {
    var params = new URLSearchParams(search);
    params.delete(prefix + TEXT_PARAM);
    for (var i = 0; i < names.length; i++) {
      params.delete(prefix + names[i]);
    }
    if (filter.text.trim()) {
      params.set(prefix + TEXT_PARAM, filter.text);
    }
    for (var j = 0; j < names.length; j++) {
      if (Object.prototype.hasOwnProperty.call(filter.choices, names[j])) {
        params.set(prefix + names[j], filter.choices[names[j]]);
      }
    }
    var query = params.toString();
    return query ? "?" + query : "";
  }

  // Header clicks cycle ascending, descending, then back to the page's own order.
  function nextSort(sort, column) {
    if (sort.column !== column) {
      return { column: column, direction: 1 };
    }
    if (sort.direction === 1) {
      return { column: column, direction: -1 };
    }
    return { column: -1, direction: 0 };
  }

  var api = {
    MIN_ROWS: MIN_ROWS,
    MIN_COLUMNS: MIN_COLUMNS,
    qualifies: qualifies,
    compareText: compareText,
    choiceColumns: choiceColumns,
    rowMatches: rowMatches,
    isActive: isActive,
    readFilter: readFilter,
    writeFilter: writeFilter,
    nextSort: nextSort,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof document === "undefined") {
    return;
  }

  function cellTexts(row) {
    return Array.prototype.map.call(row.cells, function (cell) {
      return cell.textContent.trim();
    });
  }

  // The ancestor of `node` that is a direct child of the page body. Material wraps each table
  // in two divs at runtime, and a heading is a sibling of the outer one, not of the table.
  function topLevelBlock(node, article) {
    while (node.parentElement && node.parentElement !== article) {
      node = node.parentElement;
    }
    return node;
  }

  // The nearest h2 or h3 above the table. An h3 counts because backlog.md files one table per
  // label under an h3.
  function headingAbove(block) {
    for (var el = block.previousElementSibling; el; el = el.previousElementSibling) {
      if (el.tagName === "H2" || el.tagName === "H3") {
        return el;
      }
      if (el.tagName === "H1") {
        return null;
      }
    }
    return null;
  }

  // The permalink Material appends to every heading reads as a trailing pilcrow.
  function headingText(heading) {
    return heading.textContent.replace(/¶\s*$/, "").trim();
  }

  // A heading and everything after it up to the next heading at its level or above.
  function sectionElements(heading) {
    var level = Number(heading.tagName.charAt(1));
    var elements = [heading];
    for (var el = heading.nextElementSibling; el; el = el.nextElementSibling) {
      if (/^H[1-6]$/.test(el.tagName) && Number(el.tagName.charAt(1)) <= level) {
        break;
      }
      if (el.classList.contains("table-filter")) {
        break;
      }
      elements.push(el);
    }
    return elements;
  }

  function element(tag, className, text) {
    var el = document.createElement(tag);
    if (className) {
      el.className = className;
    }
    if (text !== undefined) {
      el.textContent = text;
    }
    return el;
  }

  function setUpGroup(group, index, article) {
    var prefix = index === 0 ? "" : index + 1 + ".";
    var hideMark = String(index);
    var records = [];
    group.tables.forEach(function (table) {
      var heading = headingAbove(topLevelBlock(table, article));
      Array.prototype.forEach.call(table.tBodies, function (body) {
        Array.prototype.forEach.call(body.rows, function (row) {
          records.push({
            row: row,
            table: table,
            heading: heading,
            section: heading ? headingText(heading) : "",
            order: records.length,
          });
        });
      });
    });

    var sectionNames = new Set(
      records.map(function (r) {
        return r.section;
      })
    );
    var hasSections = sectionNames.size >= 2;
    var names = hasSections ? group.headers.concat([SECTION]) : group.headers.slice();

    function cellsOf(record) {
      var cells = cellTexts(record.row);
      if (hasSections) {
        cells.push(record.section);
      }
      return cells;
    }

    var choices = choiceColumns(names, records.map(cellsOf), hasSections ? names.length - 1 : -1);
    // The Section dropdown first: on a split page it is the split the page already makes.
    choices.sort(function (a, b) {
      return (b.name === SECTION) - (a.name === SECTION) || a.column - b.column;
    });
    var choiceNames = choices.map(function (choice) {
      return choice.name;
    });

    var bar = element("div", "table-filter");
    var text = element("input", "table-filter__text");
    text.type = "search";
    text.placeholder = "Filter rows";
    text.setAttribute("aria-label", "Filter rows by any text");
    bar.appendChild(text);

    var selects = choices.map(function (choice) {
      var label = element("label", "table-filter__choice");
      label.appendChild(element("span", null, choice.name));
      var select = element("select");
      select.appendChild(element("option", null, "All"));
      select.options[0].value = "";
      choice.values.forEach(function (pair, i) {
        var option = element("option", null, (pair[0] || "(empty)") + " (" + pair[1] + ")");
        option.value = String(i);
        select.appendChild(option);
      });
      label.appendChild(select);
      bar.appendChild(label);
      return select;
    });

    var count = element("span", "table-filter__count");
    count.setAttribute("aria-live", "polite");
    bar.appendChild(count);
    var clear = element("button", "table-filter__clear", "Clear");
    clear.type = "button";
    bar.appendChild(clear);

    var firstBlock = topLevelBlock(group.tables[0], article);
    var anchor = (hasSections && headingAbove(firstBlock)) || firstBlock;
    anchor.parentNode.insertBefore(bar, anchor);
    // extra.css offsets a row anchor's scroll position by this, so a linked row does not land
    // under the sticky bar. Set once now, because revealTarget below scrolls before any
    // observer fires, and again whenever the bar wraps.
    function recordHeight() {
      document.documentElement.style.setProperty("--table-filter-height", bar.offsetHeight + "px");
    }
    recordHeight();
    if (window.ResizeObserver) {
      new ResizeObserver(recordHeight).observe(bar);
    }

    // A dropdown column holds short category names, which the site-wide
    // `overflow-wrap: anywhere` would otherwise break mid-word: `deploy_tool|s`.
    choices.forEach(function (choice) {
      if (choice.column >= group.headers.length) {
        return;
      }
      records.forEach(function (record) {
        var cell = record.row.cells[choice.column];
        if (cell) {
          cell.classList.add("table-filter__category");
        }
      });
    });

    var filter = readFilter(window.location.search, choiceNames, prefix);
    // Drop a value the page no longer offers, rather than filter every row away on a stale link.
    choices.forEach(function (choice, i) {
      if (!Object.prototype.hasOwnProperty.call(filter.choices, choice.name)) {
        return;
      }
      var at = choice.values.findIndex(function (pair) {
        return pair[0] === filter.choices[choice.name];
      });
      if (at === -1) {
        delete filter.choices[choice.name];
      } else {
        selects[i].value = String(at);
      }
    });
    text.value = filter.text;

    function setHidden(el, hide) {
      if (hide) {
        el.hidden = true;
        el.setAttribute(HIDDEN_ATTR, hideMark);
      } else if (el.getAttribute(HIDDEN_ATTR) === hideMark) {
        el.hidden = false;
        el.removeAttribute(HIDDEN_ATTR);
      }
    }

    function apply() {
      var shown = 0;
      var perHeading = new Map();
      records.forEach(function (record) {
        var match = rowMatches(names, cellsOf(record), filter);
        setHidden(record.row, !match);
        shown += match ? 1 : 0;
        if (record.heading) {
          perHeading.set(record.heading, (perHeading.get(record.heading) || 0) + (match ? 1 : 0));
        }
      });
      if (hasSections) {
        perHeading.forEach(function (visible, heading) {
          sectionElements(heading).forEach(function (el) {
            setHidden(el, visible === 0);
          });
        });
      }
      var active = isActive(filter);
      count.textContent = active
        ? shown + " of " + records.length + " rows"
        : records.length + " rows";
      clear.disabled = !active;
    }

    function update() {
      apply();
      var search = writeFilter(window.location.search, choiceNames, prefix, filter);
      window.history.replaceState(
        window.history.state,
        "",
        window.location.pathname + search + window.location.hash
      );
    }

    function reset() {
      filter = { text: "", choices: {} };
      text.value = "";
      selects.forEach(function (select) {
        select.value = "";
      });
      update();
    }

    text.addEventListener("input", function () {
      filter.text = text.value;
      update();
    });
    selects.forEach(function (select, i) {
      select.addEventListener("change", function () {
        if (select.value === "") {
          delete filter.choices[choices[i].name];
        } else {
          filter.choices[choices[i].name] = choices[i].values[Number(select.value)][0];
        }
        update();
      });
    });
    clear.addEventListener("click", reset);

    // A link into a row this filter hides would scroll to nothing.
    function revealTarget() {
      var id = decodeURIComponent(window.location.hash.slice(1));
      var target = id && document.getElementById(id);
      if (target && target.closest("[" + HIDDEN_ATTR + '="' + hideMark + '"]')) {
        reset();
        target.scrollIntoView();
      }
    }
    window.addEventListener("hashchange", revealTarget);

    var sort = { column: -1, direction: 0 };
    function sortBy(column) {
      sort = nextSort(sort, column);
      group.tables.forEach(function (table) {
        var own = records.filter(function (r) {
          return r.table === table;
        });
        own.sort(function (a, b) {
          if (sort.column === -1) {
            return a.order - b.order;
          }
          var byValue = compareText(
            cellTexts(a.row)[sort.column] || "",
            cellTexts(b.row)[sort.column] || ""
          );
          return sort.direction * byValue || a.order - b.order;
        });
        own.forEach(function (r) {
          r.row.parentNode.appendChild(r.row);
        });
        Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, c) {
          if (c === sort.column) {
            th.setAttribute("aria-sort", sort.direction === 1 ? "ascending" : "descending");
          } else {
            th.removeAttribute("aria-sort");
          }
        });
      });
    }
    group.tables.forEach(function (table) {
      Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, c) {
        var button = element("button", "table-filter__sort");
        button.type = "button";
        button.title = "Sort by " + group.headers[c];
        while (th.firstChild) {
          button.appendChild(th.firstChild);
        }
        th.appendChild(button);
        button.addEventListener("click", function () {
          sortBy(c);
        });
      });
    });

    apply();
    revealTarget();
  }

  function run() {
    var article = document.querySelector("article.md-typeset") || document.querySelector(".md-typeset");
    if (!article) {
      return;
    }
    var groups = [];
    var byHeader = new Map();
    article.querySelectorAll("table:not([class])").forEach(function (table) {
      if (!table.tHead || !table.tHead.rows.length || !table.tBodies.length) {
        return;
      }
      var headers = cellTexts(table.tHead.rows[0]);
      var key = headers.join("\u0000");
      if (!byHeader.has(key)) {
        byHeader.set(key, { headers: headers, tables: [] });
        groups.push(byHeader.get(key));
      }
      byHeader.get(key).tables.push(table);
    });
    var index = 0;
    groups.forEach(function (group) {
      var rowCount = group.tables.reduce(function (sum, table) {
        return sum + table.tBodies[0].rows.length;
      }, 0);
      if (qualifies(group.headers.length, rowCount)) {
        setUpGroup(group, index, article);
        index += 1;
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
