"""SewFlow — локальный веб-интерфейс автоматизации швейного микро-производства.

Запуск:  python app.py  →  http://127.0.0.1:5000
"""

from pathlib import Path

from flask import Flask, redirect, render_template, request, send_from_directory, url_for

from modules import costing

ROOT = Path(__file__).resolve().parent

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


@app.get("/")
def index():
    cards = sorted((ROOT / "tech_maps").glob("*.md"))
    reports = sorted((ROOT / "cost_reports").glob("*.html"), reverse=True)
    return render_template("index.html",
                           cards=cards, reports=reports,
                           flash=request.args.get("flash", ""),
                           error=request.args.get("error", ""))


@app.post("/costing")
def make_costing():
    card = request.form.get("card", "")
    if not card:
        return redirect(url_for("index", error="Выберите техкарту"))
    try:
        res = costing.run(card)
        return redirect(url_for("index",
                                flash=f"Себестоимость: {res['total']:,.0f} ₽ · дашборд: "
                                      f"{Path(res['report']).name}"))
    except Exception as e:
        return redirect(url_for("index", error=f"Расчёт себестоимости: {e}"))


@app.get("/view/<folder>/<name>")
def view_file(folder, name):
    """Отдача сгенерированных файлов (только рабочие папки проекта)."""
    allowed = {"tech_maps": ROOT / "tech_maps",
               "cost_reports": ROOT / "cost_reports"}
    if folder not in allowed:
        return "forbidden", 403
    return send_from_directory(allowed[folder], name)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
