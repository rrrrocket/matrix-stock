document.addEventListener("DOMContentLoaded", () => {
  const dialog = document.getElementById("admin-confirm");
  if (!dialog) return;

  const titles = {
    approve: "审核注册申请",
    reject: "拒绝注册申请",
    disable: "停用用户",
    enable: "启用用户",
    reopen: "恢复注册审核",
  };
  const descriptions = {
    approve: (name) => `通过 ${name} 的注册申请后，该用户可以登录库存管理系统。`,
    reject: (name) => `拒绝 ${name} 的注册申请后，该用户将无法登录。`,
    disable: (name) => `停用 ${name} 后，该用户将无法登录。`,
    enable: (name) => `启用 ${name} 后，该用户可以重新登录。`,
    reopen: (name) => `将 ${name} 恢复为待审核状态？`,
  };

  document.querySelectorAll("[data-admin-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.adminAction;
      if (!Object.hasOwn(titles, action)) return;
      document.getElementById("admin-confirm-title").textContent = titles[action];
      document.getElementById("admin-confirm-description").textContent = descriptions[action](button.dataset.username);
      document.getElementById("admin-confirm-action").value = action;
      document.getElementById("admin-confirm-user-id").value = button.dataset.userId;
      const submit = document.getElementById("admin-confirm-submit");
      submit.textContent = action === "approve" ? "通过" : action === "reject" ? "拒绝" : "确认";
      submit.classList.toggle("danger", action === "reject" || action === "disable");
      dialog.showModal();
    });
  });
  dialog.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => dialog.close()));
});
