// 防止表单重复点击；返回页面时恢复按钮。 / Prevent duplicate clicks and restore after browser back.
document.addEventListener("submit", (event) => {
  if (!event.target.matches("[data-question-form]")) return;
  const button = event.target.querySelector("[data-question-submit]");
  button.disabled = true;
  button.textContent = "正在查找…";
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    // 回到缓存页时重新核验来源，不显示旧证据。 / Revalidate sources instead of showing a cached evidence page.
    window.location.reload();
    return;
  }
  document.querySelectorAll("[data-question-submit]").forEach(button => {
    button.disabled = false;
    button.textContent = "查找依据";
  });
});

