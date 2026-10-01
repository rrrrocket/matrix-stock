document.addEventListener("change", (event) => {
  const select = event.target;
  if (!(select instanceof HTMLSelectElement) || select.name !== "warehouse_id") return;
  const form = select.closest(".outbound-form");
  if (!form) return;
  const quantity = form.querySelector('input[name="quantity"]');
  if (!(quantity instanceof HTMLInputElement)) return;
  const stock = Number(select.selectedOptions[0]?.dataset.stock || 0);
  const remaining = Number(quantity.dataset.remaining || 0);
  const maximum = Math.min(stock, remaining);
  quantity.max = String(maximum);
  quantity.value = String(Math.max(1, Math.min(Number(quantity.value) || 1, maximum)));
});
