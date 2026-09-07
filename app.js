const $ = id => document.getElementById(id);
const escapeHTML = value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let lessons = [], selected, worker, timeout, animation, lastReport;
let shownFrames = [], shownIndex = 0, shownScene;
const saveKey = id => `pesok-course-v1:${id}`;

function saveCode() {
  if (!selected) return;
  try { localStorage.setItem(saveKey(selected.id), $("code").value); $("save-state").textContent = "Сохранено в этом браузере"; }
  catch { $("save-state").textContent = "Сохранение недоступно — скопируйте код перед закрытием"; }
}

function setBusy(busy) {
  ["run", "check", "seed", "disturbances", "reset-code", "example-code"].forEach(id => $(id).disabled = busy);
  $("stop").disabled = !busy;
  $("code").readOnly = busy;
}

function stop(message = "Выполнение остановлено.") {
  if (worker) worker.terminate();
  worker = null;
  clearTimeout(timeout);
  cancelAnimationFrame(animation);
  setBusy(false);
  if (message) $("runtime-status").textContent = message;
}

function selectLesson(lesson) {
  saveCode();
  stop(null);
  selected = lesson;
  document.querySelectorAll(".lesson-button").forEach(button => {
    button.classList.toggle("active", button.dataset.id === lesson.id);
    button.setAttribute("aria-pressed", String(button.dataset.id === lesson.id));
  });
  $("task-title").textContent = lesson.title;
  $("task-topic").textContent = lesson.topic;
  $("task-description").textContent = lesson.description;
  $("task-hint").textContent = lesson.hint;
  $("criteria").replaceChildren(...lesson.criteria.map(text => {
    const element = document.createElement("span"); element.textContent = text; return element;
  }));
  let saved;
  try { saved = localStorage.getItem(saveKey(lesson.id)); } catch {}
  $("code").value = saved ?? lesson.starter;
  $("save-state").textContent = saved === null || saved === undefined ? "Заготовка — добавьте свой алгоритм" : "Восстановлено из этого браузера";
  $("runtime-status").textContent = "Python загрузится при первом запуске. Нужен доступ в интернет.";
  $("result-body").innerHTML = '<div class="empty-state"><span>01 → 02 → 03</span><p>Допишите алгоритм и запустите проверку.</p></div>';
  $("console").hidden = true;
  $("protocol-details").hidden = true;
  $("copy-report").disabled = true;
  lastReport = null;
  shownFrames = [];
  shownScene = { width: ["obstacles", "delivery"].includes(lesson.id) ? 320 : 260,
    height: lesson.id === "obstacles" ? 200 : 120, radius: 12, obstacles: [], task: lesson.id };
  drawWorld(); drawChart(); updateTelemetry(null);
}

function showError(message) {
  stop("Проверка не завершена. Исправьте ошибку и повторите запуск.");
  $("console").hidden = false;
  $("console").textContent = message;
  $("result-body").innerHTML = '<div class="empty-state"><p>Результат не засчитан: выполнение прервано.</p></div>';
}

function start(suite) {
  stop(null); saveCode();
  const seed = Number($("seed").value);
  if (!Number.isInteger(seed) || seed < 1 || seed > 99999) { showError("Вариант должен быть целым числом от 1 до 99999."); return; }
  const source = $("code").value;
  if (source.length > 40000) { showError("Для задания допускается не более 40 000 символов кода."); return; }
  const request = { source, task: selected.id, seed, disturbances: $("disturbances").checked, suite };
  lastReport = null;
  $("copy-report").disabled = true;
  $("console").hidden = true;
  $("protocol-details").hidden = true;
  $("result-body").innerHTML = '<div class="empty-state"><span>ВЫЧИСЛЕНИЕ</span><p>Подготавливаем эксперимент…</p></div>';
  setBusy(true);
  worker = new Worker(new URL("worker.js", import.meta.url), { type: "module" });
  timeout = setTimeout(() => showError("Не удалось загрузить Python за 90 секунд. Проверьте доступ к cdn.jsdelivr.net и повторите запуск."), 90000);
  worker.onerror = event => { event.preventDefault(); showError(event.message || "Не удалось запустить Python. Проверьте подключение к интернету."); };
  worker.onmessage = ({ data }) => {
    if (data.type === "status") $("runtime-status").textContent = data.text;
    if (data.type === "running") {
      clearTimeout(timeout);
      timeout = setTimeout(() => showError("Превышен лимит вычисления 12 секунд. step() должен завершаться быстро. Уберите бесконечные циклы и time.sleep()."), 12000);
      $("runtime-status").textContent = suite ? "Выполняем пять вариантов…" : "Выполняем заезд…";
    }
    if (data.type === "error") showError(data.error);
    if (data.type === "result") {
      stop(null);
      lastReport = { ...data.result, solution: source, parameters: request,
        note: "Браузерная самопроверка. Для оценки преподаватель повторно запускает исходный код." };
      delete lastReport.parameters.source;
      renderResults(data.result);
      $("copy-report").disabled = false;
      $("runtime-status").textContent = "Расчёт завершён. Для повторяемости используйте тот же вариант и настройки возмущений.";
      if (!suite && data.result.cases[0].frames.length) play(data.result.cases[0]);
    }
  };
  worker.postMessage(request);
}

function renderResults(result) {
  const success = result.passed === result.total;
  const first = result.cases[0];
  const summary = result.total === 1 ? first.reason : `Пройдено ${result.passed} из ${result.total} вариантов`;
  let html = `<div class="summary-result ${success ? "" : "failed"}"><span class="result-icon">${success ? "✓" : "↗"}</span><div><h3>${escapeHTML(summary)}</h3><p>Модель ${escapeHTML(result.model_version)} · ${first.disturbances ? "шум, асимметрия привода и потеря телеметрии" : "номинальные условия"}</p></div></div>`;
  if (result.total === 1) {
    html += '<div class="metrics">' + [
      ["Модельное время", `${first.elapsed_s.toFixed(2)} с`],
      ["Пройденный путь", `${first.path_cm.toFixed(1)} см`],
      ["Столкновения", first.collision ? "Да" : "Нет"],
      [first.task === "servos" || first.task === "delivery" ? "Завершено фаз" : "Дистанция до правой стены*",
        first.task === "servos" || first.task === "delivery" ? first.completed_phases : `${first.final_distance_cm.toFixed(1)} см`],
    ].map(([label, value]) => `<div class="metric"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong></div>`).join("") + '</div>';
    if (!["servos", "delivery"].includes(first.task)) html += '<p class="chart-caption">* Геометрическая оценка проверяющей системы по оси X; не показание дальномера.</p>';
  } else {
    html += '<table class="results-table"><thead><tr><th>Вариант</th><th>Результат</th><th>Время</th><th>Путь</th></tr></thead><tbody>' + result.cases.map(item => `<tr><td>${item.seed}</td><td class="${item.passed ? "pass" : "fail"}">${escapeHTML(item.reason)}</td><td>${item.elapsed_s} с</td><td>${item.path_cm} см</td></tr>`).join("") + '</tbody></table>';
  }
  $("result-body").innerHTML = html;
  const logs = result.cases.map(item => [item.stdout, item.error].filter(Boolean).join("\n")).filter(Boolean).join("\n\n");
  $("console").hidden = !logs;
  $("console").textContent = logs;
  if (result.total === 1) {
    $("protocol-details").hidden = false;
    $("protocol-log").textContent = first.protocol.map(item => `${item.t.toFixed(2)} с\n→ STX ${JSON.stringify(item.tx)} ETX\n← STX ${JSON.stringify(item.rx)} ETX`).join("\n\n");
  }
}

function play(result) {
  shownScene = result.scene; shownFrames = result.frames; shownIndex = 0;
  let last = performance.now(), elapsed = 0;
  $("stop").disabled = false;
  function next(now) {
    elapsed += (now - last) / 1000 * Number($("playback-speed").value); last = now;
    shownIndex = Math.min(Math.floor(elapsed / 0.05), shownFrames.length - 1);
    const frame = shownFrames[shownIndex];
    drawWorld(frame); drawChart(); updateTelemetry(frame);
    if (shownIndex < shownFrames.length - 1) animation = requestAnimationFrame(next);
    else $("stop").disabled = true;
  }
  animation = requestAnimationFrame(next);
}

function updateTelemetry(frame) {
  $("simulation-time").textContent = `t = ${frame ? frame.t.toFixed(2) : "0.00"} с`;
  $("telemetry").innerHTML = ["fl", "fr", "laser", "rl", "rr"].map(name => {
    const value = frame?.distances[name], valid = value !== undefined && value !== 999;
    return `<div class="sensor"><span>${name.toUpperCase()}</span><strong class="${valid ? "" : "invalid"}">${valid ? value.toFixed(0) + " см" : "—"}</strong></div>`;
  }).join("");
}

function drawWorld(frame) {
  const canvas = $("world"), ctx = canvas.getContext("2d"), scene = shownScene;
  if (!scene) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#f2f4ee"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  const scale = Math.min((canvas.width - 86) / scene.width, (canvas.height - 95) / scene.height);
  const ox = (canvas.width - scene.width * scale) / 2, oy = (canvas.height - scene.height * scale) / 2;
  const xy = (x, y) => [ox + x * scale, canvas.height - oy - y * scale];
  ctx.strokeStyle = "#dfe5d8"; ctx.lineWidth = 1;
  for (let x = 0; x <= scene.width; x += 20) { ctx.beginPath(); ctx.moveTo(...xy(x, 0)); ctx.lineTo(...xy(x, scene.height)); ctx.stroke(); }
  for (let y = 0; y <= scene.height; y += 20) { ctx.beginPath(); ctx.moveTo(...xy(0, y)); ctx.lineTo(...xy(scene.width, y)); ctx.stroke(); }
  if (scene.task !== "servos") {
    const width = scene.task === "distance" ? 7 : 45;
    const x = scene.task === "distance" ? scene.width - 43 : scene.width - width;
    ctx.fillStyle = "rgba(192, 213, 155, .28)"; ctx.fillRect(...xy(x, scene.height), width * scale, scene.height * scale);
    if (scene.task === "delivery") ctx.fillRect(...xy(0, scene.height), 55 * scale, scene.height * scale);
  }
  ctx.strokeStyle = "#929e89"; ctx.lineWidth = 2;
  ctx.strokeRect(ox, oy, scene.width * scale, scene.height * scale);
  ctx.fillStyle = "#8d987e"; ctx.font = "12px monospace";
  ctx.fillText("0", ox - 4, canvas.height - oy + 22);
  ctx.fillText(`${scene.width} см → X`, ox + scene.width * scale - 90, canvas.height - oy + 22);
  scene.obstacles.forEach(([x, y, r]) => {
    ctx.beginPath(); ctx.arc(...xy(x, y), r * scale, 0, 2 * Math.PI);
    ctx.fillStyle = "#d6c1af"; ctx.fill(); ctx.strokeStyle = "#b9a08a"; ctx.lineWidth = 2; ctx.stroke();
  });
  if (!frame) {
    ctx.fillStyle = "#89957f"; ctx.font = "15px sans-serif"; ctx.textAlign = "center";
    ctx.fillText("Траектория появится после запуска", canvas.width / 2, canvas.height / 2); ctx.textAlign = "start"; return;
  }
  ctx.strokeStyle = "#bd8d68"; ctx.lineWidth = 2; ctx.beginPath();
  shownFrames.slice(0, shownIndex + 1).forEach((f, i) => { if (i === 0) ctx.moveTo(...xy(f.x, f.y)); else ctx.lineTo(...xy(f.x, f.y)); }); ctx.stroke();
  Object.values(frame.rays).forEach(([x, y, ex, ey]) => { ctx.strokeStyle = "#9cad786f"; ctx.lineWidth = 1.4; ctx.beginPath(); ctx.moveTo(...xy(x, y)); ctx.lineTo(...xy(ex, ey)); ctx.stroke(); });
  ctx.save(); ctx.translate(...xy(frame.x, frame.y)); ctx.rotate(-frame.theta);
  ctx.fillStyle = "#344f3b"; ctx.beginPath(); ctx.roundRect(-12 * scale, -10 * scale, 24 * scale, 20 * scale, 5); ctx.fill();
  ctx.fillStyle = "#232e24"; ctx.fillRect(-8 * scale, -13 * scale, 16 * scale, 4 * scale); ctx.fillRect(-8 * scale, 9 * scale, 16 * scale, 4 * scale);
  ctx.fillStyle = "#daeeaa"; ctx.beginPath(); ctx.moveTo(8 * scale, 0); ctx.lineTo(-1 * scale, -4 * scale); ctx.lineTo(-1 * scale, 4 * scale); ctx.closePath(); ctx.fill();
  ctx.restore();
  ctx.fillStyle = "#526b4a"; ctx.font = "12px monospace";
  ctx.fillText(`PWM ${frame.left} / ${frame.right}`, ox, 23);
  ctx.fillText(`SERVO ${frame.servos[0]} / ${frame.servos[1]} мкс`, ox + 215, 23);
  if (["servos", "delivery"].includes(scene.task)) ctx.fillText(`ФАЗА ${frame.phase}`, canvas.width - 115, 23);
}

function drawChart() {
  const canvas = $("chart"), ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const end = Math.max(shownFrames.length - 1, 1), h = canvas.height - 20;
  const y = value => h - Math.max(0, Math.min(255, value)) / 255 * (h - 10);
  ctx.strokeStyle = "#c8d1bc"; ctx.setLineDash([5, 5]); ctx.beginPath(); ctx.moveTo(0, y(30)); ctx.lineTo(canvas.width, y(30)); ctx.stroke(); ctx.setLineDash([]);
  for (const [color, getter] of [["#567347", f => f.distances.laser === 999 ? null : f.distances.laser], ["#c59873", f => Math.abs((f.left + f.right) / 2)]]) {
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath(); let move = true;
    shownFrames.slice(0, shownIndex + 1).forEach((f, i) => { const value = getter(f); if (value === null) { move = true; return; } const x = i / end * canvas.width; if (move) ctx.moveTo(x, y(value)); else ctx.lineTo(x, y(value)); move = false; }); ctx.stroke();
  }
}

$("code").addEventListener("input", saveCode);
$("code").addEventListener("keydown", event => {
  if (event.key === "Tab" && !$("code").readOnly) {
    event.preventDefault(); const start = event.target.selectionStart;
    event.target.setRangeText("    ", start, event.target.selectionEnd, "end"); saveCode();
  }
});
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !$("run").disabled) { event.preventDefault(); start(false); }
});
$("run").onclick = () => start(false);
$("check").onclick = () => start(true);
$("stop").onclick = () => stop();
$("reset-code").onclick = () => {
  if (confirm("Заменить текущий код заготовкой задания? Сначала скопируйте решение, если оно нужно.")) { $("code").value = selected.starter; saveCode(); }
};
$("example-code").onclick = () => {
  if (!confirm("Заменить код коротким примером движения? Он демонстрирует API, но не решает задание.")) return;
  $("code").value = 'from pesok_sdk import ESP32Client\n\nclass Controller:\n    def __init__(self):\n        self.elapsed = 0.0\n\n    def step(self, robot: ESP32Client, dt: float):\n        self.elapsed += dt\n        if self.elapsed < 2.0:\n            robot.raw_motors(120, 120)\n        else:\n            robot.stop()\n';
  saveCode();
};
$("copy-report").onclick = async () => {
  if (!lastReport) return;
  const report = { ...lastReport, cases: lastReport.cases.map(({ frames, protocol, scene, ...item }) => item) };
  const text = JSON.stringify(report, null, 2);
  try { await navigator.clipboard.writeText(text); $("runtime-status").textContent = "Отчёт и исходный код скопированы. Можно вставить их в систему сдачи работ."; }
  catch { $("console").hidden = false; $("console").textContent = text; $("runtime-status").textContent = "Буфер обмена недоступен. Выделите и скопируйте отчёт ниже."; }
};

try {
  const response = await fetch(new URL("lessons.json", import.meta.url));
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  lessons = await response.json();
  $("lessons").replaceChildren(...lessons.map(lesson => {
    const button = document.createElement("button"); button.className = "lesson-button"; button.type = "button"; button.dataset.id = lesson.id;
    button.innerHTML = `<span class="lesson-number">${escapeHTML(lesson.number)}</span><span>${escapeHTML(lesson.title)}</span>`;
    button.onclick = () => selectLesson(lesson); return button;
  }));
  selectLesson(lessons[0]);
} catch (error) {
  $("task-title").textContent = "Не удалось загрузить практикум";
  $("runtime-status").textContent = "Откройте сайт по HTTP(S). При открытии index.html как локального файла браузер блокирует загрузку модулей. " + error.message;
  $("run").disabled = $("check").disabled = true;
}
