// Python executes in a dedicated worker. Stop terminates even an infinite Python loop.
const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/pyodide.mjs";

self.onmessage = async ({ data }) => {
  try {
    self.postMessage({ type: "status", text: "Загрузка Python… Первый запуск может занять некоторое время." });
    const { loadPyodide } = await import(PYODIDE_URL);
    const pyodide = await loadPyodide({ indexURL: new URL(".", PYODIDE_URL).href });
    const names = ["pesok_sdk.py", "simulator.py"];
    const files = await Promise.all(names.map(async name => {
      const response = await fetch(new URL(name, self.location.href));
      if (!response.ok) throw new Error(`Не удалось загрузить ${name}: HTTP ${response.status}`);
      return response.text();
    }));
    names.forEach((name, index) => pyodide.FS.writeFile(name, files[index]));
    self.postMessage({ type: "running" });
    pyodide.globals.set("student_source", data.source);
    pyodide.globals.set("task_id", data.task);
    pyodide.globals.set("case_seed", data.seed);
    pyodide.globals.set("use_disturbances", data.disturbances);
    pyodide.globals.set("run_suite", data.suite);
    const result = await pyodide.runPythonAsync(`
from simulator import evaluate_json
evaluate_json(student_source, task_id, case_seed, use_disturbances, run_suite)
`);
    self.postMessage({ type: "result", result: JSON.parse(result) });
  } catch (error) {
    self.postMessage({ type: "error", error: String(error?.stack || error) });
  }
};
