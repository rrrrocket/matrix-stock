document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-live-search]").forEach((form) => {
    const target = document.getElementById(form.dataset.liveTarget);
    if (!(target instanceof HTMLElement)) return;
    let timer;
    let controller;

    const search = async () => {
      form.querySelector(".live-search-error")?.remove();
      controller?.abort();
      controller = new AbortController();
      const current = controller;
      const url = new URL(form.action || window.location.href);
      url.search = window.location.search;
      for (const [name, value] of new FormData(form)) url.searchParams.set(name, value);
      document.querySelectorAll(".admin-search input[type=search]").forEach((input) => {
        url.searchParams.set(input.name, input.value);
      });
      if (form.dataset.liveResetPage) url.searchParams.delete(form.dataset.liveResetPage);
      target.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(url, {
          headers: { "X-Requested-With": "XMLHttpRequest" }, signal: current.signal,
          credentials: "same-origin",
        });
        if (response.redirected) { window.location.assign(response.url); return; }
        if (!response.ok) throw new Error("搜索失败，请重试");
        const documentResult = new DOMParser().parseFromString(await response.text(), "text/html");
        const fresh = documentResult.getElementById(form.dataset.liveTarget);
        if (!fresh) throw new Error("搜索结果不可用，请刷新页面");
        target.innerHTML = fresh.innerHTML;
        document.dispatchEvent(new CustomEvent("stock:search-updated", { detail: { target: target.id } }));
        const panel = form.closest(".admin-panel");
        const count = panel?.querySelector(".admin-count");
        const freshCount = panel && documentResult.getElementById(panel.id)?.querySelector(".admin-count");
        if (count && freshCount) count.textContent = freshCount.textContent;
        if (document.querySelector(".admin-search")) {
          document.querySelectorAll(".admin-search input[type=search]").forEach((input) => {
            url.searchParams.set(input.name, input.value);
          });
        }
        window.history.replaceState(null, "", url.pathname + url.search + window.location.hash);
      } catch (error) {
        if (error.name !== "AbortError") {
          const message = document.createElement("span");
          message.className = "live-search-error";
          message.setAttribute("role", "alert");
          message.textContent = error.message;
          form.append(message);
        }
      } finally {
        if (controller === current) target.removeAttribute("aria-busy");
      }
    };

    form.addEventListener("input", (event) => {
      if (!(event.target instanceof HTMLInputElement) || !["text", "search"].includes(event.target.type)) return;
      clearTimeout(timer);
      controller?.abort();
      timer = window.setTimeout(search, 250);
    });
    form.addEventListener("change", (event) => {
      if (!(event.target instanceof HTMLSelectElement)) return;
      clearTimeout(timer);
      search();
    });
    form.addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(timer); search(); });
  });
});
