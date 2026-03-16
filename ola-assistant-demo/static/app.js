const form = document.getElementById("chat-form");
const input = document.getElementById("message-input");
const chatLog = document.getElementById("chat-log");
const sendButton = document.getElementById("send-button");
const statusText = document.getElementById("status-text");

function addMessage(role, text) {
  const article = document.createElement("article");
  article.className = `message ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;

  article.appendChild(bubble);
  chatLog.appendChild(article);
  chatLog.scrollTop = chatLog.scrollHeight;
  return bubble;
}

function setBusy(isBusy) {
  sendButton.disabled = isBusy;
  input.disabled = isBusy;
  statusText.textContent = isBusy ? "正在请求本地 Codex..." : "本地模式已就绪";
}

async function sendMessage(message) {
  addMessage("user", message);
  const pendingBubble = addMessage("assistant", "正在思考，请稍候...");
  setBusy(true);

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ message }),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "请求失败");
    }

    pendingBubble.textContent = data.reply;
  } catch (error) {
    pendingBubble.textContent = `请求失败：${error.message}`;
  } finally {
    setBusy(false);
    input.focus();
    chatLog.scrollTop = chatLog.scrollHeight;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) {
    return;
  }

  input.value = "";
  await sendMessage(message);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});
