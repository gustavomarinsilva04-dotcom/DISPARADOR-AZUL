"""
Plataforma de envio WhatsApp (API oficial da Meta) - Azul Empréstimo Noroeste.
Versão para celular: login com senha, app instalável na tela inicial (PWA),
teste de envio, checklist de configuração, fila com pausa e limite diário.
"""
import hashlib
import hmac
import json
import os
import random
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import requests
from flask import Flask, Response, jsonify, redirect, request, session

TOKEN = os.environ.get("WA_TOKEN", "")
PHONE_ID = os.environ.get("WA_PHONE_ID", "")
TEMPLATE = os.environ.get("WA_TEMPLATE", "")
LANG = os.environ.get("WA_LANG", "pt_BR")
VERIFY = os.environ.get("WA_VERIFY_TOKEN", "")
SENHA = os.environ.get("APP_SENHA", "")
LIMITE_DIA = int(os.environ.get("LIMITE_DIA", "100"))
ESPERA_MIN = float(os.environ.get("ESPERA_MIN", "8"))
ESPERA_MAX = float(os.environ.get("ESPERA_MAX", "25"))
DATA_DIR = os.environ.get("DATA_DIR", ".")

CODIGOS_PARAR = {130429, 131048, 131056, 368}  # Meta limitando o número: para tudo
MAX_FALHAS_SEGUIDAS = 5
SAIR = {"parar", "sair", "cancelar", "stop", "nao quero", "não quero", "remover"}
URL = "https://graph.facebook.com/v21.0/{}/messages"

if not SENHA or len(SENHA) < 8:
    raise SystemExit("Defina APP_SENHA com pelo menos 8 caracteres.")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("disp:" + SENHA).encode()).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SEGURO", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
estado = {"rodando": False, "msg": "Parado"}
trava = threading.Lock()
tentativas = {"n": 0, "ate": 0.0}


# ---------- banco ----------
def conn():
    db = sqlite3.connect(os.path.join(DATA_DIR, "plataforma.db"), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def iniciar_banco():
    db = conn()
    db.executescript(
        """CREATE TABLE IF NOT EXISTS contatos(
             telefone TEXT PRIMARY KEY, nome TEXT, status TEXT DEFAULT 'pendente',
             detalhe TEXT DEFAULT '', quando TEXT DEFAULT '');
           CREATE TABLE IF NOT EXISTS optout(telefone TEXT PRIMARY KEY, quando TEXT);"""
    )
    db.commit()
    db.close()


def normaliza(tel):
    d = re.sub(r"\D", "", tel or "")
    if len(d) in (10, 11):
        d = "55" + d
    return d if d.startswith("55") and len(d) in (12, 13) else None


def enviados_hoje(db):
    return db.execute(
        "SELECT COUNT(*) FROM contatos WHERE status='enviado' AND quando LIKE ?", (date.today().isoformat() + "%",)
    ).fetchone()[0]


# ---------- login ----------
LIVRES = {"/login", "/manifest.webmanifest", "/sw.js", "/saude"}


@app.before_request
def exigir_login():
    p = request.path
    if p in LIVRES or p.startswith("/webhook") or p.startswith("/static/"):
        return None
    if session.get("ok"):
        return None
    if p.startswith("/api/"):
        return jsonify(ok=False, erro="Sessão expirada. Entre de novo."), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = ""
    if request.method == "POST":
        if time.time() < tentativas["ate"]:
            erro = "Muitas tentativas. Aguarde 1 minuto."
        elif hmac.compare_digest(request.form.get("senha", "").encode(), SENHA.encode()):
            session.permanent = True
            session["ok"] = True
            tentativas["n"] = 0
            return redirect("/")
        else:
            tentativas["n"] += 1
            if tentativas["n"] >= 5:
                tentativas.update(n=0, ate=time.time() + 60)
            erro = "Senha incorreta."
    return Response(LOGIN.replace("__ERRO__", erro), mimetype="text/html")


@app.get("/sair")
def sair():
    session.clear()
    return redirect("/login")


@app.get("/saude")
def saude():
    return "ok"


@app.get("/manifest.webmanifest")
def manifest():
    m = {
        "name": "Disparador · Azul Empréstimo Noroeste",
        "short_name": "Disparador",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0e1626",
        "theme_color": "#128c4a",
        "icons": [
            {"src": "/static/icone-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icone-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return Response(json.dumps(m), mimetype="application/manifest+json")


@app.get("/sw.js")
def sw():
    js = 'self.addEventListener("install",e=>self.skipWaiting());self.addEventListener("fetch",()=>{});'
    return Response(js, mimetype="application/javascript")


# ---------- envio ----------
def enviar_template(tel, nome):
    primeiro = (nome.split() or [""])[0] or "tudo bem"
    corpo = {
        "messaging_product": "whatsapp",
        "to": tel,
        "type": "template",
        "template": {
            "name": TEMPLATE,
            "language": {"code": LANG},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": primeiro}]}],
        },
    }
    return requests.post(URL.format(PHONE_ID), json=corpo, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)


def esperar(segundos):
    for _ in range(int(segundos)):
        if not estado["rodando"]:
            return
        time.sleep(1)


def trabalhador():
    falhas = 0
    while estado["rodando"]:
        db = conn()
        try:
            if enviados_hoje(db) >= LIMITE_DIA:
                estado["msg"] = f"Limite de {LIMITE_DIA} envios de hoje atingido. Continue amanhã."
                break
            c = db.execute(
                "SELECT telefone, nome FROM contatos WHERE status='pendente' "
                "AND telefone NOT IN (SELECT telefone FROM optout) LIMIT 1"
            ).fetchone()
            if not c:
                estado["msg"] = "Fila concluída."
                break
            estado["msg"] = f"Enviando para {c['nome'] or c['telefone']}..."
            agora = datetime.now().isoformat(timespec="seconds")
            try:
                r = enviar_template(c["telefone"], c["nome"])
                dados = r.json()
            except Exception as e:
                falhas += 1
                estado["msg"] = f"Erro de conexão: {e}"
                if falhas >= MAX_FALHAS_SEGUIDAS:
                    estado["msg"] = "Parado: muitas falhas de conexão seguidas."
                    break
                esperar(10)
                continue
            if r.ok:
                msg_id = dados.get("messages", [{}])[0].get("id", "")
                db.execute("UPDATE contatos SET status='enviado', detalhe=?, quando=? WHERE telefone=?",
                           (msg_id, agora, c["telefone"]))
                db.commit()
                falhas = 0
                esperar(random.uniform(ESPERA_MIN, ESPERA_MAX))
            else:
                erro = dados.get("error", {})
                codigo, texto = erro.get("code"), erro.get("message", "")
                db.execute("UPDATE contatos SET status='falhou', detalhe=?, quando=? WHERE telefone=?",
                           (f"{codigo}: {texto}", agora, c["telefone"]))
                db.commit()
                if codigo in CODIGOS_PARAR:
                    estado["msg"] = "Parado: a Meta sinalizou limite/qualidade. Aguarde e revise antes de continuar."
                    break
                falhas += 1
                if falhas >= MAX_FALHAS_SEGUIDAS:
                    estado["msg"] = "Parado: muitas falhas seguidas. Confira o modelo e a lista."
                    break
        finally:
            db.close()
    estado["rodando"] = False
    if estado["msg"].startswith("Enviando"):
        estado["msg"] = "Pausado."


# ---------- API ----------
@app.post("/api/importar")
def importar():
    j = request.get_json(silent=True) or {}
    todos_autorizaram = bool(j.get("todos_autorizaram"))
    db = conn()
    novos = invalidos = sem_aut = 0
    for linha in (j.get("dados") or "").splitlines():
        linha = linha.strip()
        if not linha or "telefone" in linha.lower():
            continue
        p = [s.strip() for s in re.split(r"[;\t]|,(?=\s*[\d(+])", linha)]
        nome, tel = p[0], (p[1] if len(p) > 1 else "")
        if len(p) == 1 and re.fullmatch(r"[\d\s()+-]+", nome):
            nome, tel = "", nome
        aut = (p[2].lower() in ("sim", "s", "1", "true")) if len(p) > 2 else todos_autorizaram
        t = normaliza(tel)
        if not t:
            invalidos += 1
            continue
        if not aut:
            sem_aut += 1
            continue
        if db.execute("SELECT 1 FROM optout WHERE telefone=?", (t,)).fetchone():
            continue
        novos += db.execute("INSERT OR IGNORE INTO contatos(telefone, nome) VALUES (?,?)", (t, nome)).rowcount
    db.commit()
    db.close()
    return jsonify(ok=True, novos=novos, invalidos=invalidos, sem_autorizacao=sem_aut)


@app.post("/api/teste")
def teste():
    if not (TOKEN and PHONE_ID and TEMPLATE):
        return jsonify(ok=False, erro="Faltam WA_TOKEN, WA_PHONE_ID ou WA_TEMPLATE no servidor."), 400
    t = normaliza((request.get_json(silent=True) or {}).get("telefone", ""))
    if not t:
        return jsonify(ok=False, erro="Telefone inválido. Use DDD + número."), 400
    try:
        r = enviar_template(t, "Teste")
        d = r.json()
    except Exception as e:
        return jsonify(ok=False, erro=f"Sem conexão com a Meta: {e}"), 502
    if r.ok:
        return jsonify(ok=True)
    e = d.get("error", {})
    return jsonify(ok=False, erro=f"{e.get('code')}: {e.get('message', '')}"), 400


@app.post("/api/iniciar")
def iniciar():
    if not (TOKEN and PHONE_ID and TEMPLATE):
        return jsonify(ok=False, erro="Faltam WA_TOKEN, WA_PHONE_ID ou WA_TEMPLATE no servidor."), 400
    with trava:
        if not estado["rodando"]:
            estado["rodando"] = True
            estado["msg"] = "Iniciando..."
            threading.Thread(target=trabalhador, daemon=True).start()
    return jsonify(ok=True)


@app.post("/api/pausar")
def pausar():
    estado["rodando"] = False
    estado["msg"] = "Pausando..."
    return jsonify(ok=True)


@app.post("/api/refazer_falhas")
def refazer():
    db = conn()
    db.execute("UPDATE contatos SET status='pendente' WHERE status='falhou'")
    db.commit()
    db.close()
    return jsonify(ok=True)


@app.post("/api/limpar_fila")
def limpar():
    db = conn()
    db.execute("DELETE FROM contatos WHERE status='pendente'")
    db.commit()
    db.close()
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    db = conn()
    cont = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) n FROM contatos GROUP BY status")}
    ultimos = [dict(r) for r in db.execute(
        "SELECT nome, telefone, status, detalhe FROM contatos ORDER BY quando DESC LIMIT 40")]
    hoje = enviados_hoje(db)
    db.close()
    cfg = {"Token da Meta": bool(TOKEN), "ID do número": bool(PHONE_ID),
           "Modelo de mensagem": bool(TEMPLATE), "Webhook (PARAR)": bool(VERIFY)}
    return jsonify(rodando=estado["rodando"], msg=estado["msg"], cont=cont, hoje=hoje,
                   limite=LIMITE_DIA, cfg=cfg, ultimos=ultimos)


# ---------- webhook (respostas PARAR) ----------
@app.get("/webhook")
def webhook_verificar():
    if VERIFY and request.args.get("hub.verify_token") == VERIFY:
        return request.args.get("hub.challenge", ""), 200
    return "token inválido", 403


@app.post("/webhook")
def webhook_receber():
    dados = request.get_json(silent=True) or {}
    db = conn()
    for e in dados.get("entry", []):
        for m in e.get("changes", []):
            for msg in m.get("value", {}).get("messages", []):
                texto = (msg.get("text", {}).get("body") or msg.get("button", {}).get("text") or "").strip().lower()
                if texto in SAIR:
                    tel = msg.get("from", "")
                    db.execute("INSERT OR REPLACE INTO optout VALUES (?,?)",
                               (tel, datetime.now().isoformat(timespec="seconds")))
                    db.execute("UPDATE contatos SET status='saiu' WHERE telefone=?", (tel,))
    db.commit()
    db.close()
    return "ok", 200


# ---------- páginas ----------
CABECA = """<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#128c4a"><link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/static/icone-192.png"><link rel="apple-touch-icon" href="/static/icone-180.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Disparador"><title>Disparador · Azul Empréstimo Noroeste</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--ink:#12213a;--mute:#5d6b82;--line:#dbe1ea;--ac:#128c4a;--acink:#fff;--bad:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#0e1626;--card:#16223a;--ink:#e8edf6;--mute:#93a1ba;--line:#26344f;--ac:#2fbf71;--acink:#06210f;--bad:#ff8a7a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-text-size-adjust:100%}
input,textarea,button{font:inherit}
button{font-weight:600;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:12px 14px;min-height:46px;cursor:pointer}
button.main{background:var(--ac);color:var(--acink);border-color:var(--ac)}button:disabled{opacity:.45}
input[type=password],input[type=tel],textarea{width:100%;border:1px solid var(--line);border-radius:12px;padding:12px;background:var(--bg);color:var(--ink)}
</style>"""

LOGIN = """<!DOCTYPE html><html lang="pt-BR"><head>""" + CABECA + """<style>
main{max-width:380px;margin:0 auto;padding:max(48px,env(safe-area-inset-top)) 20px 20px}h1{font-size:1.5rem;margin:0 0 4px}
p{color:var(--mute);margin:0 0 20px}.erro{color:var(--bad);margin:0 0 12px}button{width:100%;margin-top:12px}
</style></head><body><main><h1>Disparador</h1><p>Azul Empréstimo Noroeste</p>
<form method="post"><input type="password" name="senha" placeholder="Senha" autocomplete="current-password" autofocus required>
<div class="erro">__ERRO__</div><button class="main">Entrar</button></form></main></body></html>"""

PAGINA = """<!DOCTYPE html><html lang="pt-BR"><head>""" + CABECA + """<style>
main{max-width:720px;margin:0 auto;padding:max(16px,env(safe-area-inset-top)) 14px calc(96px + env(safe-area-inset-bottom))}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}h1{font-size:1.35rem;margin:0}
header a{color:var(--mute);font-size:.9rem}
section,details{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
summary{font-weight:700;cursor:pointer;min-height:28px}details[open] summary{margin-bottom:10px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px}
.n{background:var(--bg);border-radius:10px;padding:8px 10px}.n b{display:block;font-size:1.35rem}.n span{color:var(--mute);font-size:.8rem}
.bar{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin:8px 0}.bar i{display:block;height:100%;background:var(--ac);width:0;transition:width .3s}
.mut{color:var(--mute)}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.row>*{flex:1 1 140px}
textarea{min-height:120px}label{display:flex;gap:10px;align-items:flex-start;margin-top:10px}label input{margin-top:5px;width:20px;height:20px}
.item{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-top:1px solid var(--line)}.item small{display:block;color:var(--mute)}
.enviado{color:var(--ac)}.falhou{color:var(--bad)}.ok{color:var(--ac)}.no{color:var(--bad)}
.barra{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 14px calc(10px + env(safe-area-inset-bottom));display:flex;gap:10px;max-width:720px;margin:0 auto}
.barra button{flex:1}
</style></head><body><main>
<header><h1>Disparador</h1><a href="/sair">Sair</a></header>
<section><div class="grid" id="cards"></div><div class="bar"><i id="prog"></i></div><div id="estado" class="mut">Carregando...</div>
<div class="row"><button id="ref">Reenviar falhas</button><button id="lim">Limpar fila</button></div></section>
<details id="dcfg"><summary>Configuração</summary><div id="cfg"></div>
<div class="mut" style="margin-top:8px">Envie um teste para o seu número antes de começar:</div>
<input type="tel" id="tel" placeholder="Seu WhatsApp com DDD" style="margin-top:8px">
<div class="row"><button class="main" id="tst">Enviar teste</button></div><div id="tmsg" class="mut" style="margin-top:8px"></div></details>
<details><summary>Importar contatos</summary>
<textarea id="dados" placeholder="nome;telefone;autorizado&#10;Maria Silva;11988887777;sim"></textarea>
<label><input type="checkbox" id="todos"><span>Confirmo que todos autorizaram receber mensagens (use se a lista não tiver a coluna "autorizado").</span></label>
<div class="row"><button id="arq">Escolher arquivo CSV</button><input type="file" id="file" accept=".csv,.txt" hidden>
<button class="main" id="imp">Adicionar à fila</button></div><div id="impmsg" class="mut" style="margin-top:8px"></div></details>
<section><b>Últimos contatos</b><div id="lista"></div></section>
</main>
<div class="barra"><button class="main" id="ini">Iniciar envio</button><button id="pau">Pausar</button></div>
<script>
const $=i=>document.getElementById(i);
const post=(u,b)=>fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})}).then(r=>{if(r.status===401){location="/login";throw 0}return r.json()});
async function atualizar(){
  const r=await fetch("/api/status");if(r.status===401){location="/login";return}
  const s=await r.json(),c=s.cont,tot=Object.values(c).reduce((a,b)=>a+b,0),env=c.enviado||0;
  $("cards").innerHTML="";[["Pendentes",c.pendente||0],["Enviados",env],["Falhas",c.falhou||0],["Saíram",c.saiu||0],["Hoje",s.hoje+"/"+s.limite]].forEach(([k,v])=>{const d=document.createElement("div");d.className="n";d.innerHTML="<b></b><span></span>";d.children[0].textContent=v;d.children[1].textContent=k;$("cards").appendChild(d)});
  $("prog").style.width=tot?(env/tot*100)+"%":"0";
  $("estado").textContent=(s.rodando?"Rodando · ":"")+s.msg;
  $("ini").disabled=s.rodando;$("pau").disabled=!s.rodando;
  const falta=Object.values(s.cfg).some(v=>!v);$("cfg").innerHTML="";
  Object.entries(s.cfg).forEach(([k,v])=>{const d=document.createElement("div");d.className="item";d.innerHTML="<span></span><b></b>";d.children[0].textContent=k;d.children[1].textContent=v?"✓ ok":"✗ falta";d.children[1].className=v?"ok":"no";$("cfg").appendChild(d)});
  if(falta&&!$("dcfg").dataset.o){$("dcfg").open=true;$("dcfg").dataset.o=1}
  $("lista").innerHTML="";if(!s.ultimos.length)$("lista").innerHTML='<div class="mut" style="padding-top:8px">Nenhum contato ainda. Importe uma lista.</div>';
  s.ultimos.forEach(u=>{const d=document.createElement("div");d.className="item";d.innerHTML="<div><span></span><small></small></div><b></b>";
    d.querySelector("span").textContent=u.nome||"(sem nome)";d.querySelector("small").textContent="+"+u.telefone+(u.status==="falhou"?" · "+u.detalhe:"");
    d.querySelector("b").textContent=u.status;d.querySelector("b").className=u.status;$("lista").appendChild(d)});
}
$("ini").onclick=async()=>{const r=await post("/api/iniciar");if(!r.ok)alert(r.erro);atualizar()};
$("pau").onclick=async()=>{await post("/api/pausar");atualizar()};
$("ref").onclick=async()=>{await post("/api/refazer_falhas");atualizar()};
$("lim").onclick=async()=>{if(confirm("Apagar todos os contatos pendentes da fila?")){await post("/api/limpar_fila");atualizar()}};
$("tst").onclick=async()=>{$("tmsg").textContent="Enviando...";const r=await post("/api/teste",{telefone:$("tel").value});$("tmsg").textContent=r.ok?"Teste enviado. Confira o seu WhatsApp.":"Falhou: "+r.erro};
$("arq").onclick=()=>$("file").click();
$("file").onchange=e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=()=>{$("dados").value=String(r.result).replace(/\\r/g,"")};r.readAsText(f)};
$("imp").onclick=async()=>{const r=await post("/api/importar",{dados:$("dados").value,todos_autorizaram:$("todos").checked});
  $("impmsg").textContent=r.novos+" adicionados · "+r.invalidos+" telefones inválidos · "+r.sem_autorizacao+" ignorados por falta de autorização";$("dados").value="";atualizar()};
if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js");
atualizar();setInterval(atualizar,3000);
</script></body></html>"""


@app.get("/")
def pagina():
    return Response(PAGINA, mimetype="text/html")


iniciar_banco()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)
