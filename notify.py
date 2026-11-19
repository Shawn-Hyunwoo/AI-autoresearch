# Slack 웹훅 알림. 주소는 ~/.config/autoresearch/slack_webhook(권한 600) 또는 환경변수 SLACK_WEBHOOK_URL.
# 주소가 없거나 네트워크가 끊겨도 연구 실행은 멈추지 않는다.
import json
import os
import re
import unicodedata
import urllib.request
from pathlib import Path

WEBHOOK_FILE = Path.home() / ".config" / "autoresearch" / "slack_webhook"

# Slack은 LaTeX를 렌더링하지 않고 _…_를 기울임꼴로 바꾼다. 그래서 수식은 유니코드로 바꿔 보낸다.
SUB = str.maketrans("0123456789aehijklmnoprstuvx+-=()", "₀₁₂₃₄₅₆₇₈₉ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ₊₋₌₍₎")
SUP = str.maketrans("0123456789+-=()abcdefghijklmnoprstuvwxyz", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ")
LATEX = {"sum": "Σ", "prod": "Π", "int": "∫", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "neq": "≠", "ne": "≠",
         "approx": "≈", "sim": "∼", "cdot": "·", "times": "×", "infty": "∞", "sqrt": "√", "pm": "±", "to": "→",
         "rightarrow": "→", "in": "∈", "partial": "∂", "nabla": "∇", "langle": "⟨", "rangle": "⟩", "dots": "…",
         "ldots": "…", "cdots": "⋯", "max": "max", "min": "min", "log": "log", "exp": "exp", "lVert": "‖", "rVert": "‖",
         "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "theta": "θ",
         "kappa": "κ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ",
         "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Sigma": "Σ",
         "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω"}


def width(s):
    # 고정폭 글꼴에서의 표시 폭. 한글 같은 전각 문자는 2칸
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def scripts(s):
    # x_1 → x₁, x_{i+1} → xᵢ₊₁, x^2 → x². 모든 글자를 바꿀 수 있을 때만 바꾼다 (max_iterations 같은 이름은 그대로)
    def rep(m, table):
        body = m.group(2).strip("{}")
        conv = body.translate(table)
        return m.group(1) + conv if all(a != b for a, b in zip(body, conv)) else m.group(0)
    s = re.sub(r"(?<![A-Za-z0-9_])([A-Za-zα-ωΑ-Ω])_(\{[^{}]{1,6}\}|[0-9]{1,2}(?![0-9A-Za-z_])|[a-z](?![A-Za-z0-9_]))",
               lambda m: rep(m, SUB), s)
    return re.sub(r"(\S)\^(\{[^{}]{1,6}\}|[0-9]{1,2}|[a-z](?![A-Za-z]))", lambda m: rep(m, SUP), s)


def latex(src):
    # $…$ 안의 흔한 LaTeX를 유니코드로. 다 바꾸지 못하면 None
    s = re.sub(r"\\(?:mathrm|text|mathbf|mathit|operatorname|bar|hat)\{([^{}]*)\}", r"\1", src)
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", s)
    s = re.sub(r"\\(?:left|right|big|Big)(?![A-Za-z])|\\[,;!]", "", s)
    s = re.sub(r"\\([A-Za-z]+)", lambda k: LATEX.get(k[1], k[0]), s)
    s = scripts(s).replace("{", "").replace("}", "")
    return None if re.search(r"[\\_^]", s) else s


def plain_math(text):
    # 코드 블록(표) 안처럼 서식이 없는 곳: 수식만 유니코드로
    text = re.sub(r"\$\$?(?!\s)([^$]+?)(?<!\s)\$\$?", lambda m: latex(m[1]) or m[1], text)
    return scripts(text)


def inline_math(seg):
    # 인라인 코드 밖의 글: 수식을 유니코드로 바꾸고, 그래도 밑줄이 남은 단어는 코드 표시로 감싸 기울임꼴을 막는다
    seg = re.sub(r"\$\$?(?!\s)([^$]+?)(?<!\s)\$\$?", lambda m: latex(m[1]) or f"`{m[1]}`", seg)
    parts = seg.split("`")
    for i in range(0, len(parts), 2):
        p = scripts(parts[i])
        parts[i] = re.sub(r"[A-Za-z0-9./~:-]*_[A-Za-z0-9_./~:-]*", lambda m: f"`{m[0]}`", p)
    return "`".join(parts)


def to_mrkdwn(md):
    # 마크다운을 Slack mrkdwn으로: **굵게** → *굵게*, 제목 → 굵은 줄, [글](주소) → <주소|글>,
    # 표 → 칸을 맞춘 코드 블록 (Slack에는 표가 없다), ```python → ``` (언어 표시는 Slack에서 글자로 보인다), 수식 → 유니코드
    out, table, in_code = [], [], False

    def flush():
        rows = [[plain_math(c.strip()) for c in ln.strip().strip("|").split("|")] for ln in table
                if not re.fullmatch(r"\|?[\s:|-]+\|?", ln.strip())]
        table.clear()
        if not rows:
            return
        n = max(len(r) for r in rows)
        rows = [r + [""] * (n - len(r)) for r in rows]
        w = [max(width(r[i]) for r in rows) for i in range(n)]
        out.append("```")
        out.extend("  ".join(c + " " * (w[i] - width(c)) for i, c in enumerate(r)).rstrip() for r in rows)
        out.append("```")

    for line in md.splitlines():
        if line.strip().startswith("```"):
            flush()
            in_code = not in_code
            out.append("```")
            continue
        if in_code:
            out.append(line)
            continue
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        flush()
        line = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        line = re.sub(r"\[([^\]]+)\]\((\S+?)\)", r"<\2|\1>", line)
        out.append(inline_math(line))
    flush()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def send(message):
    url = WEBHOOK_FILE.read_text().strip() if WEBHOOK_FILE.exists() else os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return False
    data = json.dumps({"text": to_mrkdwn(message)[:35000]}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


if __name__ == "__main__":
    md = "# 제목\n\n**굵게** 와 `코드`\n\n| 항목 | 값 |\n|---|---|\n| 시간 | 0.5초 |\n| guard | 통과 |\n\n```python\nx = 1\n```"
    got = to_mrkdwn(md)
    assert got.startswith("*제목*") and "*굵게*" in got and "**" not in got and "|" not in got, got
    assert "```\n항목   값\n시간   0.5초\nguard  통과\n```" in got and "```python" not in got, got
    cases = {
        "x_1, …, x_n은 실수다.": "x₁, …, xₙ은 실수다.",
        "S = Σx_i, Q = Σx_i²": "S = Σxᵢ, Q = Σxᵢ²",
        "등호는 $\\sum_{i=1}^n x_i^2 \\le n$일 때": "등호는 Σᵢ₌₁ⁿ xᵢ² ≤ n일 때",
        "$\\frac{1}{n}\\sum_i x_i$": "(1)/(n)Σᵢ xᵢ",
        "파일 round_1_verdict.json과 max_iterations": "파일 `round_1_verdict.json`과 `max_iterations`",
        "이미 `a_b` 코드": "이미 `a_b` 코드",
        "x^{2n} 그리고 2^k": "x²ⁿ 그리고 2ᵏ",
    }
    for src, want in cases.items():
        assert to_mrkdwn(src) == want, (src, to_mrkdwn(src))
    assert "xᵢ" in to_mrkdwn("| a | x_i |\n|---|---|\n| 1 | 2 |")
    print("self-check ok")
