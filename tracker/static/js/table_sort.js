/** Client-side per-column sorting for dashboard tables. */
(function () {
  function cellText(row, colIndex) {
    const cell = row.cells[colIndex];
    if (!cell) {
      return "";
    }
    const explicit = cell.getAttribute("data-sort-value");
    if (explicit !== null) {
      return explicit.trim();
    }
    return cell.textContent.trim();
  }

  function sortKey(text) {
    if (!text || text === "—") {
      return { empty: true, value: "" };
    }

    const isoDates = text.match(/\d{4}-\d{2}-\d{2}/g);
    if (isoDates) {
      return { empty: false, value: isoDates.sort().pop() };
    }

    const dayMatch = text.match(/\bday\s+(\d+)\b/i);
    if (dayMatch) {
      return { empty: false, value: dayMatch[1].padStart(8, "0") };
    }

    const pendingDays = text.match(/\bdays\s+([\d,\s]+)/i);
    if (pendingDays) {
      const nums = pendingDays[1]
        .split(",")
        .map((part) => parseInt(part.trim(), 10))
        .filter((n) => !Number.isNaN(n));
      if (nums.length) {
        return { empty: false, value: String(Math.min(...nums)).padStart(8, "0") };
      }
    }

    const bracketList = text.match(/^\[([^\]]*)\]$/);
    if (bracketList) {
      const nums = bracketList[1]
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean)
        .map(Number);
      if (nums.length && nums.every((n) => !Number.isNaN(n))) {
        return { empty: false, value: String(Math.max(...nums)).padStart(8, "0") };
      }
    }

    return { empty: false, value: text.toLowerCase() };
  }

  function compareRows(rowA, rowB, colIndex) {
    const a = sortKey(cellText(rowA, colIndex));
    const b = sortKey(cellText(rowB, colIndex));
    if (a.empty && !b.empty) {
      return 1;
    }
    if (!a.empty && b.empty) {
      return -1;
    }
    if (a.value < b.value) {
      return -1;
    }
    if (a.value > b.value) {
      return 1;
    }
    return 0;
  }

  function clearSortState(table) {
    table.querySelectorAll(".sortable-header").forEach((header) => {
      delete header.dataset.sortDir;
      header.classList.remove("sort-asc", "sort-desc");
      header.setAttribute("aria-sort", "none");
    });
  }

  function sortTable(table, colIndex, header) {
    const current = header.dataset.sortDir;
    const direction = current === "asc" ? "desc" : "asc";
    clearSortState(table);
    header.dataset.sortDir = direction;
    header.classList.add(direction === "asc" ? "sort-asc" : "sort-desc");
    header.setAttribute("aria-sort", direction === "asc" ? "ascending" : "descending");

    const tbody = table.tBodies[0];
    const rows = Array.from(tbody.rows);
    rows.sort((rowA, rowB) => {
      const cmp = compareRows(rowA, rowB, colIndex);
      return direction === "asc" ? cmp : -cmp;
    });
    rows.forEach((row) => tbody.appendChild(row));
  }

  function initTable(table) {
    if (!table.tHead || !table.tBodies[0]) {
      return;
    }

    const headerRow = table.tHead.rows[0];
    Array.from(headerRow.cells).forEach((header, index) => {
      if (header.classList.contains("no-sort")) {
        return;
      }

      header.classList.add("sortable-header");
      header.setAttribute("role", "button");
      header.setAttribute("tabindex", "0");
      header.setAttribute("aria-sort", "none");

      header.addEventListener("click", () => sortTable(table, index, header));
      header.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sortTable(table, index, header);
        }
      });
    });
  }

  function initAllTables() {
    document.querySelectorAll("table.sortable-table").forEach(initTable);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAllTables);
  } else {
    initAllTables();
  }
})();
