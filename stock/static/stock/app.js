const refreshOrderAvailability = () => {
  const selectedTotals = new Map();
  document.querySelectorAll(".batch-order-select:checked").forEach((checkbox) => {
    const article = checkbox.closest("[data-order-line-id]");
    const form = article?.querySelector(".outbound-form");
    const warehouse = form?.elements.namedItem("warehouse_id")?.value;
    const quantity = Number(form?.elements.namedItem("quantity")?.value || 0);
    if (!article || !warehouse) return;
    const key = `${warehouse}:${article.dataset.sku}`;
    selectedTotals.set(key, (selectedTotals.get(key) || 0) + quantity);
  });
  let batchValid = true;
  document.querySelectorAll(".outbound-form").forEach((form) => {
    const article = form.closest("[data-order-line-id]");
    const select = form.elements.namedItem("warehouse_id");
    const input = form.elements.namedItem("quantity");
    const hint = form.querySelector(".outbound-stock-hint");
    const button = form.querySelector('button[type="submit"]');
    if (!(select instanceof HTMLSelectElement) || !(input instanceof HTMLInputElement) || !hint) return;
    const stock = Number(select.selectedOptions[0]?.dataset.stock || 0);
    const orderRemaining = Number(input.dataset.remaining || 0);
    const quantity = Number(input.value);
    const maximum = Math.min(stock, orderRemaining);
    input.max = String(maximum);
    const valid = input.value !== "" && Number.isInteger(quantity) && quantity >= 1 && quantity <= maximum;
    if (button instanceof HTMLButtonElement) button.disabled = !valid;
    const selected = Boolean(article?.querySelector(".batch-order-select:checked"));
    const total = selected ? selectedTotals.get(`${select.value}:${article.dataset.sku}`) || 0 : quantity;
    const batchEnough = !selected || total <= stock;
    if (selected && (!valid || !batchEnough)) batchValid = false;
    hint.classList.toggle("is-error", !valid || !batchEnough);
    if (input.value === "") hint.textContent = `当前可用 ${stock} 件，请填写出库数量`;
    else if (!Number.isInteger(quantity) || quantity < 1) hint.textContent = "出库数量须为正整数";
    else if (quantity > orderRemaining) hint.textContent = `本单待处理 ${orderRemaining} 件`;
    else if (quantity > stock) hint.textContent = `当前仓库仅剩 ${stock} 件，超出 ${quantity - stock} 件`;
    else if (!batchEnough) hint.textContent = `本批同货号共 ${total} 件，当前仓库仅剩 ${stock} 件`;
    else hint.textContent = `${selected ? "本批" : "本次"}出库后，仓库剩余 ${stock - total} 件`;
  });
  const batchButton = document.querySelector('#batch-order-outbound-form button[type="submit"]');
  if (batchButton instanceof HTMLButtonElement) batchButton.disabled = !batchValid;
  return batchValid;
};
refreshOrderAvailability();
document.addEventListener("stock:search-updated", (event) => {
  if (event.detail?.target === "order-results") refreshOrderAvailability();
});
document.addEventListener("input", (event) => {
  if (event.target instanceof HTMLInputElement && event.target.matches('.outbound-form input[name="quantity"]')) refreshOrderAvailability();
});
document.addEventListener("change", (event) => {
  if (event.target instanceof HTMLSelectElement && event.target.matches('.outbound-form select[name="warehouse_id"]')) refreshOrderAvailability();
  if (event.target instanceof HTMLInputElement && event.target.classList.contains("batch-order-select")) refreshOrderAvailability();
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const prompt = form.dataset.confirm;
  if (prompt && !window.confirm(prompt)) event.preventDefault();
});

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  target.closest("[data-dismiss-message]")?.closest(".flash")?.remove();
});

const showInlineToast = (message, kind) => {
  let stack = document.querySelector(".flash-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.className = "flash-stack";
    stack.setAttribute("role", "status");
    document.querySelector("main.page")?.prepend(stack);
  }
  stack.querySelector("[data-inline-toast]")?.remove();
  const toast = document.createElement("div");
  toast.className = `flash flash-${kind}`;
  toast.dataset.inlineToast = "";
  const label = document.createElement("span");
  label.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "flash-close";
  close.dataset.dismissMessage = "";
  close.setAttribute("aria-label", "关闭提示");
  close.textContent = "×";
  toast.append(label, close);
  stack.append(toast);
  setTimeout(() => toast.remove(), 4000);
};

const inventoryInlineFields = {
  "inventory-quantity-form": { input: "new_quantity", original: "expected_quantity", result: "quantity" },
  "inventory-cost-form": { input: "unit_cost", original: null, result: "unit_cost" },
  "inventory-note-form": { input: "note", original: "expected_note", result: "note" },
  "inventory-expiry-form": { input: "expires_on", original: "expected_expiry", result: "expires_on" },
};
const inventoryInlineField = (form) => Object.entries(inventoryInlineFields)
  .find(([className]) => form.classList.contains(className))?.[1];
const inventoryInlineOriginal = (form, field) => field.original
  ? form.elements.namedItem(field.original)?.value ?? ""
  : form.dataset.savedValue ?? form.elements.namedItem(field.input)?.value ?? "";
const finishInventoryInlineEdit = (form) => form.classList.remove("is-editing");

// Delegated events continue to work when live search replaces the table body.
document.addEventListener("dblclick", (event) => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const display = target.closest(".inventory-inline-value");
  const form = display?.closest(".inventory-inline-form");
  if (!(form instanceof HTMLFormElement) || form.dataset.saving === "true") return;
  const field = inventoryInlineField(form);
  const input = field && form.elements.namedItem(field.input);
  if (!(input instanceof HTMLInputElement)) return;
  if (!field.original) form.dataset.savedValue = input.value;
  form.classList.add("is-editing");
  input.focus();
  if (input.type !== "date") input.select();
});
document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  if (target.classList.contains("inventory-inline-value") && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    target.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    return;
  }
  if (!(target instanceof HTMLInputElement) || !target.closest(".inventory-inline-form")) return;
  const form = target.closest(".inventory-inline-form");
  if (event.key === "Escape") {
    event.preventDefault();
    target.value = inventoryInlineOriginal(form, inventoryInlineField(form));
    finishInventoryInlineEdit(form);
    target.blur();
  } else if (event.key === "Enter") {
    event.preventDefault();
    form.requestSubmit();
  }
});
document.addEventListener("focusout", (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement)) return;
  const form = input.closest(".inventory-inline-form.is-editing");
  if (!(form instanceof HTMLFormElement) || form.dataset.saving === "true") return;
  form.requestSubmit();
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.classList.contains("inventory-inline-form")) return;
  event.preventDefault();
  if (form.dataset.saving === "true") return;
  const field = inventoryInlineField(form);
  const input = field && form.elements.namedItem(field.input);
  const original = field?.original ? form.elements.namedItem(field.original) : null;
  if (!(input instanceof HTMLInputElement)) return;
  if (!input.checkValidity()) {
    input.reportValidity();
    return;
  }
  if (input.value === inventoryInlineOriginal(form, field)) {
    finishInventoryInlineEdit(form);
    return;
  }
  form.dataset.saving = "true";
  try {
    const response = await fetch(form.action, {
      method: "POST", body: new FormData(form),
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 409 && original instanceof HTMLInputElement) {
        const current = field.result === "quantity" ? result.current_quantity : result.current_value;
        if (current !== undefined && current !== null) original.value = String(current);
      }
      throw new Error(result.error || "保存失败，请重试");
    }
    const saved = String(result[field.result] ?? "");
    input.value = saved;
    if (original instanceof HTMLInputElement) original.value = saved;
    else form.dataset.savedValue = saved;
    const display = form.querySelector(".inventory-inline-value");
    if (display) display.textContent = saved || "—";
    const updatedCell = form.closest("tr")?.lastElementChild;
    if (updatedCell) updatedCell.textContent = result.updated_at;
    finishInventoryInlineEdit(form);
    showInlineToast(result.message, "success");
  } catch (error) {
    showInlineToast(error.message || "保存失败，请重试", "error");
  } finally {
    delete form.dataset.saving;
  }
});

const orderImagePreview = document.getElementById("order-image-preview");
if (orderImagePreview instanceof HTMLDialogElement) {
  const image = orderImagePreview.querySelector("img");
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const trigger = target.closest("[data-order-image]");
    if (trigger instanceof HTMLButtonElement && image instanceof HTMLImageElement) {
      image.src = trigger.dataset.orderImage || "";
      image.alt = `${trigger.dataset.orderSku || "商品"} 主图`;
      orderImagePreview.showModal();
    } else if (target.closest("[data-close-order-image]") || target === orderImagePreview) {
      orderImagePreview.close();
    }
  });
  orderImagePreview.addEventListener("close", () => {
    if (image instanceof HTMLImageElement) image.removeAttribute("src");
  });
}

const batchForm = document.getElementById("batch-inbound-form");
if (batchForm instanceof HTMLFormElement) {
  const rows = document.getElementById("batch-inbound-rows");
  const warehouse = batchForm.elements.namedItem("warehouse_id");
  const defaultQuantity = batchForm.elements.namedItem("default_quantity");
  const addRow = (after = null) => {
    if (!(rows instanceof HTMLTableSectionElement) || rows.rows.length >= 100) return null;
    const row = rows.rows[0].cloneNode(true);
    if (!(row instanceof HTMLTableRowElement)) return null;
    row.querySelectorAll("input").forEach((input) => { input.value = ""; });
    if (after instanceof HTMLTableRowElement) after.after(row);
    else rows.append(row);
    return row;
  };
  document.getElementById("add-batch-row")?.addEventListener("click", () => {
    addRow()?.querySelector('input[name="sku"]')?.focus();
  });
  rows?.addEventListener("click", (event) => {
    if (!(event.target instanceof Element) || !event.target.closest("[data-remove-batch-row]")) return;
    const row = event.target.closest("tr");
    if (!(row instanceof HTMLTableRowElement)) return;
    if (rows.rows.length > 1) row.remove();
    else row.querySelectorAll("input").forEach((input) => { input.value = ""; });
  });
  rows?.addEventListener("paste", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.name !== "sku") return;
    const values = event.clipboardData?.getData("text")?.split(/\r?\n/)
      .map((line) => line.split("\t").map((value) => value.trim()))
      .filter((cells) => cells[0]) || [];
    if (values.length < 2 && values[0]?.length < 2) return;
    event.preventDefault();
    let row = input.closest("tr");
    for (const [index, cells] of values.entries()) {
      if (index) row = addRow(row);
      if (!(row instanceof HTMLTableRowElement)) break;
      row.querySelector('input[name="sku"]').value = cells[0];
      if (cells[1]) row.querySelector('input[name="quantity"]').value = cells[1];
      if (cells[2]) row.querySelector('input[name="unit_cost"]').value = cells[2];
      if (cells[3]) row.querySelector('input[name="note"]').value = cells[3];
      if (cells[4]) row.querySelector('input[name="expires_on"]').value = cells[4];
    }
  });
  try {
    if (warehouse instanceof HTMLSelectElement) {
      warehouse.value = localStorage.getItem("stock.batchWarehouse") || warehouse.value;
      if (!warehouse.value && warehouse.options.length === 2) warehouse.selectedIndex = 1;
    }
    if (defaultQuantity instanceof HTMLInputElement) defaultQuantity.value = localStorage.getItem("stock.batchQuantity") || defaultQuantity.value;
  } catch (_) { /* Browser storage may be unavailable. */ }
  batchForm.addEventListener("change", () => {
    try {
      if (warehouse instanceof HTMLSelectElement) localStorage.setItem("stock.batchWarehouse", warehouse.value);
      if (defaultQuantity instanceof HTMLInputElement && defaultQuantity.validity.valid) {
        localStorage.setItem("stock.batchQuantity", defaultQuantity.value);
      }
    } catch (_) { /* Browser storage may be unavailable. */ }
  });
}

const batchOrderForm = document.getElementById("batch-order-outbound-form");
if (batchOrderForm instanceof HTMLFormElement) {
  const selected = () => [...document.querySelectorAll(".batch-order-select:checked")];
  document.addEventListener("stock:search-updated", (event) => {
    if (event.detail?.target === "order-results") document.getElementById("batch-order-count").textContent = "已选 0 条";
  });
  document.addEventListener("change", (event) => {
    if (event.target instanceof HTMLInputElement && event.target.classList.contains("batch-order-select")) {
      document.getElementById("batch-order-count").textContent = `已选 ${selected().length} 条`;
    }
  });
  batchOrderForm.addEventListener("submit", (event) => {
    if (!refreshOrderAvailability()) {
      event.preventDefault();
      alert("所选订单商品数量超过仓库可用库存，请调整后再提交");
      return;
    }
    const rows = selected().map((checkbox) => {
      const article = checkbox.closest("[data-order-line-id]");
      const form = article?.querySelector(".outbound-form");
      return {
        line_id: article?.dataset.orderLineId,
        warehouse_id: form?.elements.namedItem("warehouse_id")?.value,
        quantity: form?.elements.namedItem("quantity")?.value,
        tracking_number: form?.elements.namedItem("tracking_number")?.value,
        operation_id: form?.elements.namedItem("operation_id")?.value,
      };
    });
    if (!rows.length) { event.preventDefault(); alert("请选择订单商品"); return; }
    if (rows.some((row) => !row.tracking_number || !row.warehouse_id || !row.quantity)) {
      event.preventDefault(); alert("请填写所选商品的仓库、数量和国内快递单号"); return;
    }
    batchOrderForm.elements.namedItem("rows").value = JSON.stringify(rows);
    if (!confirm(`确认出库 ${rows.length} 条订单商品？`)) event.preventDefault();
  });
}

const manualForm = document.getElementById("manual-outbound-form");
if (manualForm instanceof HTMLFormElement) {
  const warehouse = manualForm.elements.namedItem("warehouse_id");
  const search = document.getElementById("outbound-sku-search");
  const results = document.getElementById("outbound-search-results");
  const rows = document.getElementById("manual-outbound-rows");
  let searchTimer;
  const money = (value) => (Math.round((value + Number.EPSILON) * 100) / 100).toFixed(2);
  const update = () => {
    let quantity = 0, amount = 0;
    let valid = true, rowCount = 0;
    rows.querySelectorAll("tr[data-sku]").forEach((row) => {
      rowCount += 1;
      const input = row.querySelector('input[name="quantity"]');
      const count = Number(input.value) || 0;
      const available = Number(row.dataset.available || 0);
      const hint = row.querySelector(".outbound-stock-hint");
      const rowValid = input.value !== "" && Number.isInteger(count) && count >= 1 && count <= available;
      if (!rowValid) valid = false;
      hint.classList.toggle("is-error", !rowValid);
      input.classList.toggle("stock-over-limit", !rowValid);
      if (input.value === "") hint.textContent = `当前可用 ${available} 件，请填写数量`;
      else if (!Number.isInteger(count) || count < 1) hint.textContent = "出库数量须为正整数";
      else if (count > available) hint.textContent = `当前仓库仅剩 ${available} 件，超出 ${count - available} 件`;
      else hint.textContent = `出库后剩余 ${available - count} 件`;
      quantity += count;
      const lineAmount = Math.round((count * Number(row.dataset.cost || 0) + Number.EPSILON) * 100) / 100;
      amount += lineAmount;
      row.querySelector("[data-subtotal]").textContent = `¥${money(lineAmount)}`;
    });
    document.getElementById("manual-outbound-total-quantity").textContent = String(quantity);
    document.getElementById("manual-outbound-total-amount").textContent = money(amount);
    const submit = manualForm.querySelector('button[type="submit"]');
    if (submit instanceof HTMLButtonElement) submit.disabled = !rowCount || !valid;
  };
  const runSearch = async () => {
    results.replaceChildren();
    if (!warehouse.value || !search.value.trim()) return;
    try {
      const response = await fetch(`/outbound/search/?warehouse_id=${encodeURIComponent(warehouse.value)}&q=${encodeURIComponent(search.value.trim())}`);
      if (!response.ok) return;
      const data = await response.json();
      for (const item of data.items) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "manual-search-option";
        button.textContent = `${item.sku} · 可用 ${item.available} 件 · 成本 ${item.unit_cost === null ? "未设置" : `¥${item.unit_cost}`}`;
        if (item.unit_cost === null) {
          button.disabled = true;
          button.title = "请先在仓库库存中设置成本单价";
        }
        button.addEventListener("click", () => {
          if (rows.querySelectorAll("tr[data-sku]").length >= 100 || [...rows.querySelectorAll("tr[data-sku]")].some((row) => row.dataset.sku === item.sku)) return;
          rows.querySelector(".empty-cell")?.closest("tr")?.remove();
          const row = document.createElement("tr");
          row.dataset.sku = item.sku;
          row.dataset.cost = item.unit_cost || "0";
          row.dataset.available = String(item.available);
          const skuCell = row.insertCell();
          skuCell.textContent = item.sku;
          const hiddenSku = document.createElement("input");
          hiddenSku.type = "hidden"; hiddenSku.name = "sku"; hiddenSku.value = item.sku; skuCell.append(hiddenSku);
          row.insertCell().textContent = String(item.available);
          const quantityCell = row.insertCell();
          const quantityInput = document.createElement("input");
          quantityInput.name = "quantity"; quantityInput.type = "number"; quantityInput.min = "1";
          quantityInput.max = String(item.available); quantityInput.step = "1"; quantityInput.value = "1";
          quantityInput.required = true; quantityInput.addEventListener("input", update); quantityCell.append(quantityInput);
          const remainingHint = document.createElement("small");
          remainingHint.className = "outbound-stock-hint";
          remainingHint.setAttribute("aria-live", "polite");
          quantityCell.append(remainingHint);
          row.insertCell().textContent = item.unit_cost === null ? "未设置" : `¥${item.unit_cost}`;
          const subtotal = row.insertCell(); subtotal.dataset.subtotal = "";
          const remove = document.createElement("button"); remove.type = "button"; remove.className = "text-button danger-text";
          remove.textContent = "移除"; remove.addEventListener("click", () => { row.remove(); update(); });
          row.insertCell().append(remove);
          rows.append(row); search.value = ""; results.replaceChildren(); update();
        });
        results.append(button);
      }
    } catch (_) { /* Network error: server validates the submitted form. */ }
  };
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 200); });
  warehouse.addEventListener("change", () => { rows.innerHTML = '<tr><td colspan="6" class="empty-cell">搜索货号后添加</td></tr>'; search.value = ""; results.replaceChildren(); update(); });
  manualForm.addEventListener("submit", (event) => {
    const items = [...rows.querySelectorAll("tr[data-sku]")];
    if (!items.length) { event.preventDefault(); alert("请先添加出库货号"); return; }
    if (items.some((row) => !row.querySelector('input[name="quantity"]').checkValidity())) return;
    if (!confirm(`确认从 ${warehouse.selectedOptions[0]?.textContent || "仓库"} 出库 ${document.getElementById("manual-outbound-total-quantity").textContent} 件，成本总价 ¥${document.getElementById("manual-outbound-total-amount").textContent}？`)) event.preventDefault();
  });
}
