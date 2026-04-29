"""
dashboard/app.py  —  Radar de Prospecção · Eixo Estratégico
Multi-usuário + GitHub (remoto) com fallback para vault local.

Localmente:  lê Radar/leads/*.md  do vault Obsidian
No Railway:  lê repositório GitHub via Contents API

Uso local:
    cd dashboard
    .venv/bin/streamlit run app.py --server.port 8502
"""

import streamlit as st
import os, glob, re, yaml, base64, requests, pandas as pd
from pathlib import Path
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
VAULT_PATH   = "/Users/virmecati/Documents/meus projetos"
LEADS_DIR    = os.path.join(VAULT_PATH, "Radar", "leads")
SUMMARY_FILE = os.path.join(VAULT_PATH, "Radar", "_ultimo-refresh.md")

VEREDITO_ICON = {"Prioritário":"🔴","Qualificado":"🟡","Monitorar":"🟢","Ignorar":"⚪"}
STATUS_ICON   = {"novo":"🆕","qualificado":"✅","sdr_acionado":"📱",
                 "em_conversa":"💬","cliente":"🏆","arquivado":"🗄️"}

# ── AUTH — secrets.toml (local) ou variável DASHBOARD_USERS (Railway) ────────
def _load_users():
    # 1. Tenta secrets.toml
    try:
        return dict(st.secrets.get("users", {}))
    except Exception:
        pass
    # 2. Fallback: variável de ambiente DASHBOARD_USERS=usuario:senha,usuario2:senha2
    raw = os.environ.get("DASHBOARD_USERS", "")
    if raw:
        users = {}
        for pair in raw.split(","):
            parts = pair.strip().split(":", 1)
            users[parts[0].strip()] = parts[1].strip() if len(parts) > 1 else ""
        return users
    return {}

def login_page():
    st.set_page_config(page_title="Login · Radar", page_icon="🔐", layout="centered")
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown("## 🔍 Radar de Prospecção")
        st.markdown("**Eixo Estratégico**")
        st.divider()
        with st.form("login"):
            usuario = st.text_input("Usuário")
            senha   = st.text_input("Senha", type="password")
            ok      = st.form_submit_button("Entrar", use_container_width=True)
            if ok:
                users = _load_users()
                if usuario in users and users[usuario] == senha:
                    st.session_state.auth  = True
                    st.session_state.user  = usuario
                    st.rerun()
                else:
                    st.error("Usuário ou senha incorretos.")

def require_auth():
    if not st.session_state.get("auth"):
        login_page()
        st.stop()

# ── DATA SOURCE — GitHub ou vault local ───────────────────────────────────────
def _gh_cfg():
    gh = st.secrets.get("github", {})
    token = gh.get("token", "")
    owner = gh.get("owner", "")
    repo  = gh.get("repo", "")
    path  = gh.get("path", "leads")
    if not (token and owner and repo and not token.startswith("ghp_xxxx")):
        return None
    return {"token": token, "owner": owner, "repo": repo, "path": path}

def _has_github():
    return _gh_cfg() is not None

def _gh_headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

# ── GitHub ────────────────────────────────────────────────────────────────────
def _gh_api(method, url, token, json=None):
    r = requests.request(method, url, headers=_gh_headers(token), json=json, timeout=10)
    if r.ok:
        return r.json()
    return None

def _parse_fm(text):
    if not text.startswith("---"): return {}, text
    end = text.find("---", 3)
    if end == -1: return {}, text
    try:    meta = yaml.safe_load(text[3:end]) or {}
    except: meta = {}
    return meta, text[end+3:].strip()

@st.cache_data(ttl=60)
def load_leads_github():
    gh = _gh_cfg()
    if not gh: return []
    base = f"https://api.github.com/repos/{gh['owner']}/{gh['repo']}/contents/{gh['path']}"
    listing = _gh_api("GET", base, gh["token"])
    if not listing or not isinstance(listing, list):
        return []
    leads = []
    for item in listing:
        if not item.get("name", "").endswith(".md"):
            continue
        file_data = _gh_api("GET", item["url"], gh["token"])
        if not file_data:
            continue
        try:
            content = base64.b64decode(file_data["content"]).decode("utf-8")
        except Exception:
            continue
        meta, body = _parse_fm(content)
        if not meta:
            continue
        if meta.get("status") == "arquivado":
            continue
        tipos = meta.get("tipos_necessidade", [])
        if isinstance(tipos, str):
            tipos = [t.strip() for t in tipos.split(",")]
        leads.append({
            "ieo":           int(meta.get("ieo_score", 0)),
            "titulo":        str(meta.get("titulo", item["name"]))[:90],
            "fonte":         str(meta.get("fonte", "—")),
            "veredito":      str(meta.get("veredito", "—")),
            "status":        str(meta.get("status", "novo")),
            "tipos":         tipos if tipos else [],
            "data_noticia":  str(meta.get("data_noticia", ""))[:10],
            "data_deteccao": str(meta.get("data_deteccao", ""))[:10],
            "url":           str(meta.get("url", "")),
            "justificativa": str(meta.get("justificativa", "")),
            "resumo":        body[:400],
            "_gh_url":       item["url"],
            "_gh_sha":       file_data.get("sha", ""),
            "_gh_content":   content,
            "_source":       "github",
        })
    return sorted(leads, key=lambda x: x["ieo"], reverse=True)

def set_status_github(lead, new_status):
    gh = _gh_cfg()
    if not gh:
        return
    updated = re.sub(r"^status:.*$", f"status: {new_status}",
                     lead["_gh_content"], flags=re.MULTILINE)
    b64 = base64.b64encode(updated.encode("utf-8")).decode("utf-8")
    _gh_api("PUT", lead["_gh_url"], gh["token"], json={
        "message": f"radar: status → {new_status} ({lead['titulo'][:40]})",
        "content": b64,
        "sha": lead["_gh_sha"],
    })
    st.cache_data.clear()

# ── Vault local ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=30)
def load_leads_local():
    if not os.path.isdir(LEADS_DIR): return []
    leads = []
    for fp in sorted(glob.glob(os.path.join(LEADS_DIR, "*.md")), reverse=True):
        try:
            text = Path(fp).read_text(encoding="utf-8")
            meta, body = _parse_fm(text)
            if not meta: continue
            if meta.get("status") == "arquivado": continue
            tipos = meta.get("tipos_necessidade", [])
            if isinstance(tipos, str): tipos = [t.strip() for t in tipos.split(",")]
            leads.append({
                "ieo":           int(meta.get("ieo_score", 0)),
                "titulo":        str(meta.get("titulo", Path(fp).stem))[:90],
                "fonte":         str(meta.get("fonte", "—")),
                "veredito":      str(meta.get("veredito", "—")),
                "status":        str(meta.get("status", "novo")),
                "tipos":         tipos if tipos else [],
                "data_noticia":  str(meta.get("data_noticia", ""))[:10],
                "data_deteccao": str(meta.get("data_deteccao", ""))[:10],
                "url":           str(meta.get("url", "")),
                "justificativa": "",
                "resumo":        body[:400],
                "_fp":           fp,
                "_source":       "local",
            })
        except: pass
    return sorted(leads, key=lambda x: x["ieo"], reverse=True)

def set_status_local(fp, new_status):
    text = Path(fp).read_text(encoding="utf-8")
    text = re.sub(r"^status:.*$", f"status: {new_status}", text, flags=re.MULTILINE)
    Path(fp).write_text(text, encoding="utf-8")
    st.cache_data.clear()

# ── Router ────────────────────────────────────────────────────────────────────
def load_leads():
    return load_leads_github() if _has_github() else load_leads_local()

def set_status(lead, new_status):
    if lead["_source"] == "github":
        set_status_github(lead, new_status)
    else:
        set_status_local(lead["_fp"], new_status)

def get_summary():
    if _has_github():
        return str(datetime.today().date()), "—"
    try:
        meta, _ = _parse_fm(Path(SUMMARY_FILE).read_text(encoding="utf-8"))
        return meta.get("data", "—"), int(meta.get("total_processados", 0))
    except: return "—", 0

# ── SDR ───────────────────────────────────────────────────────────────────────
def gerar_sdr(lead):
    t = lead["titulo"][:50]
    tipos = lead["tipos"]
    if "captou_recursos" in tipos:
        return f"Olá! Vi a notícia sobre *{t}*.\n\nSou da *Eixo Estratégico* — consultoria especializada em eficiência operacional pós-captação.\n\nEmpresas nessa fase frequentemente precisam transformar o capital em estrutura sustentável. Em 30 min mostro um diagnóstico inicial gratuito com os principais riscos de escala.\n\nTeria disponibilidade esta semana?"
    if "precisa_captar" in tipos:
        return f"Olá! Acompanhei as movimentações de *{t}*.\n\nSou da *Eixo Estratégico*. Ajudamos empresas a estruturar a tese de captação com base em dados operacionais — o que aumenta atratividade para investidores.\n\nPosso mostrar como funciona em 20 min?"
    if "dores_operacionais" in tipos:
        return f"Olá! Vi a notícia sobre *{t}*.\n\nSou da *Eixo Estratégico* — diagnóstico de gargalos operacionais e planos de ação quantitativos.\n\nPosso enviar um estudo rápido com os principais pontos de melhoria do seu setor?"
    if "cenarios" in tipos:
        return f"Olá! Acompanhei as movimentações de *{t}*.\n\nSou da *Eixo Estratégico*. Desenvolvemos estudos de cenários prospectivos (horizonte 3–15 anos) para apoiar decisões em momentos de transição.\n\nPosso enviar um exemplo aplicado ao seu setor?"
    return f"Olá! Vi a notícia sobre *{t}*.\n\nSou da *Eixo Estratégico* — consultoria estratégica com foco em decisões baseadas em dados.\n\nPosso mostrar um diagnóstico inicial gratuito em 30 min. Teria disponibilidade?"

# ── UI ────────────────────────────────────────────────────────────────────────
def sidebar(leads):
    with st.sidebar:
        st.markdown(f"### 👤 {st.session_state.get('user','')}")
        st.markdown("**Radar de Prospecção · EE**")
        fonte = "☁️ GitHub" if _has_github() else "💾 Vault local"
        st.caption(fonte)
        st.divider()
        st.markdown("### Filtros")
        vs  = ["Todos"] + sorted({l["veredito"] for l in leads})
        ss  = ["Todos"] + sorted({l["status"]   for l in leads})
        ts  = ["Todos"] + sorted({t for l in leads for t in l["tipos"]})
        sv  = st.selectbox("Veredito",         vs)
        ss_ = st.selectbox("Status",           ss)
        st_ = st.selectbox("Tipo necessidade", ts)
        mi  = st.slider("IEO mínimo", 0, 100, 0, step=5)
        st.divider()
        if st.button("🔄 Recarregar", use_container_width=True):
            st.cache_data.clear(); st.rerun()
        if st.button("🚪 Sair", use_container_width=True):
            st.session_state.clear(); st.rerun()
    return sv, ss_, st_, mi

def metrics(leads):
    c = st.columns(6)
    c[0].metric("Total",          len(leads))
    c[1].metric("🔴 Prioritários", sum(1 for l in leads if l["veredito"]=="Prioritário"))
    c[2].metric("🟡 Qualificados", sum(1 for l in leads if l["veredito"]=="Qualificado"))
    c[3].metric("🆕 Novos",        sum(1 for l in leads if l["status"]=="novo"))
    c[4].metric("💬 Em conversa",  sum(1 for l in leads if l["status"] in ("sdr_acionado","em_conversa")))
    c[5].metric("🏆 Clientes",     sum(1 for l in leads if l["status"]=="cliente"))

def render_leads(leads):
    if not leads:
        st.info("Nenhum lead com esses filtros."); return
    for i, l in enumerate(leads):
        vi = VEREDITO_ICON.get(l["veredito"], "⚪")
        si = STATUS_ICON.get(l["status"], "❓")
        tipos_md = " ".join(f"`{t}`" for t in l["tipos"]) if l["tipos"] else "—"
        header = f"{vi} **[IEO {l['ieo']}]** {l['titulo'][:72]}  ·  {si} *{l['status']}*"
        expanded = i == 0 and l["veredito"] == "Prioritário"
        with st.expander(header, expanded=expanded):
            ci, ca = st.columns([3, 1])
            with ci:
                st.markdown(f"**Fonte:** {l['fonte']}  ·  **Detectado:** {l['data_deteccao']}  ·  **Notícia:** {l['data_noticia']}")
                st.markdown(f"**Tipo:** {tipos_md}")
                if l["justificativa"]: st.caption(f"Sinais: {l['justificativa']}")
                if l["url"]: st.markdown(f"[🔗 Ver notícia]({l['url']})")
                if l["resumo"]:
                    st.divider()
                    clean = "\n".join(ln for ln in l["resumo"].split("\n")
                                     if ln.strip() and not ln.startswith(("##", "- [", "---", "[[")))
                    st.markdown(clean[:600])
            with ca:
                st.markdown("**Ações**")
                k = f"l{i}"
                if l["status"] == "novo":
                    if st.button("✅ Qualificar", key=f"{k}_q", use_container_width=True):
                        set_status(l, "qualificado"); st.rerun()
                if l["status"] in ("novo", "qualificado"):
                    if st.button("📱 Acionar SDR", key=f"{k}_s", use_container_width=True):
                        set_status(l, "sdr_acionado")
                        st.session_state[f"sdr_{i}"] = True; st.rerun()
                if l["status"] == "sdr_acionado":
                    if st.button("💬 Em conversa", key=f"{k}_c", use_container_width=True):
                        set_status(l, "em_conversa"); st.rerun()
                if l["status"] == "em_conversa":
                    if st.button("🏆 Cliente!", key=f"{k}_cl", use_container_width=True):
                        set_status(l, "cliente"); st.rerun()
                if l["status"] not in ("cliente", "arquivado"):
                    if st.button("🗄️ Arquivar", key=f"{k}_a", use_container_width=True):
                        set_status(l, "arquivado"); st.rerun()
                if st.session_state.get(f"sdr_{i}") or l["status"] in ("sdr_acionado", "em_conversa"):
                    st.divider()
                    st.markdown("**Mensagem SDR:**")
                    st.text_area("", gerar_sdr(l), height=190, key=f"{k}_msg",
                                 label_visibility="collapsed")
                    st.caption("Copie → WhatsApp / Tomik CRM")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    require_auth()
    st.set_page_config(page_title="Radar · EE", page_icon="🔍",
                       layout="wide", initial_sidebar_state="expanded")

    leads = load_leads()
    last_date, total = get_summary()
    st.title("🔍 Radar de Prospecção · Eixo Estratégico")
    fonte_label = "GitHub ☁️" if _has_github() else "Vault local 💾"
    st.caption(f"Último refresh: **{last_date}**  ·  {total} notícias processadas  "
               f"·  {len(leads)} leads  ·  Fonte: {fonte_label}")
    st.divider()

    sv, ss, st_, mi = sidebar(leads)
    metrics(leads)
    st.divider()

    filtered = [l for l in leads
                if (sv == "Todos" or l["veredito"] == sv)
                and (ss == "Todos" or l["status"] == ss)
                and (st_ == "Todos" or st_ in l["tipos"])
                and l["ieo"] >= mi]

    st.subheader(f"Leads — {len(filtered)} de {len(leads)}")
    render_leads(filtered)

if __name__ == "__main__":
    main()
