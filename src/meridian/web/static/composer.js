// Enter sends the message; Shift+Enter adds a new line (live probe 2026-10-20: Enter only added a line).
document.addEventListener("keydown", (e) => {
  const box = e.target;
  // keyCode 229: Safari's Enter that confirms an IME composition
  if (box && box.id === "message" && e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    const send = box.form && box.form.querySelector("button");
    if (box.value.trim() && send && !send.disabled) box.form.requestSubmit();
  }
});
