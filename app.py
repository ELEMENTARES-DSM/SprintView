import os
import re
import json
import requests
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import logging
import time
import uuid
import traceback

# ==========================================
# 0. SISTEMA DE DEBUGGING E DIAGNÓSTICO
# ==========================================
load_dotenv()

DEBUG_MODE = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
LOG_LEVEL = logging.DEBUG if DEBUG_MODE else logging.INFO
RUN_ID = str(uuid.uuid4())[:8]

logger = logging.getLogger("SprintDashboard")
logger.setLevel(LOG_LEVEL)

if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(LOG_LEVEL)
    formatter = logging.Formatter(f'%(asctime)s [%(levelname)s] [RunID:{RUN_ID}] %(name)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

def handle_http_error(response, service, op_id):
    """Traduz erros HTTP em diagnósticos legíveis e imediatos."""
    status = response.status_code
    if status == 401:
        logger.error(f"[OpID:{op_id}] {service} - HTTP 401: Falha de Autenticação. Token expirado, revogado ou e-mail incorreto.")
    elif status == 403:
        logger.error(f"[OpID:{op_id}] {service} - HTTP 403: Proibido. Seu token é válido, mas não tem permissão para acessar este recurso.")
    elif status == 404:
        logger.error(f"[OpID:{op_id}] {service} - HTTP 404: Não Encontrado. Verifique se o BOARD_ID, repositório ou URL estão corretos.")
    elif status == 429:
        logger.error(f"[OpID:{op_id}] {service} - HTTP 429: Rate Limit atingido. O provedor bloqueou temporariamente por excesso de chamadas.")
    else:
        logger.error(f"[OpID:{op_id}] {service} - HTTP {status}: Erro genérico da API - {response.text}")

def mask_sensitive_data(data):
    if isinstance(data, dict):
        masked = {}
        for k, v in data.items():
            if any(sec in str(k).lower() for sec in ['token', 'password', 'secret', 'email', 'auth', 'authorization']):
                masked[k] = "********"
            else:
                masked[k] = mask_sensitive_data(v)
        return masked
    elif isinstance(data, list):
        return [mask_sensitive_data(i) for i in data]
    elif isinstance(data, str):
        if "Bearer " in data: return data.split("Bearer ")[0] + "Bearer ********"
        if "Basic " in data: return data.split("Basic ")[0] + "Basic ********"
    return data

class TraceOperation:
    def __init__(self, op_name, **context_kwargs):
        self.op_name = op_name
        self.op_id = str(uuid.uuid4())[:8]
        self.start_time = None
        self.context = mask_sensitive_data(context_kwargs)

    def __enter__(self):
        self.start_time = time.time()
        ctx_str = f" | Context: {self.context}" if self.context else ""
        logger.debug(f"[OpID:{self.op_id}] Operação '{self.op_name}' iniciada.{ctx_str}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.start_time
        if exc_type is None:
            logger.debug(f"[OpID:{self.op_id}] '{self.op_name}' concluída ({elapsed:.2f}s).")
        else:
            logger.error(f"[OpID:{self.op_id}] Falha na '{self.op_name}' ({elapsed:.2f}s). Erro: {str(exc_val)}")
            logger.error(f"[OpID:{self.op_id}] Stack trace:\n" + "".join(traceback.format_exception(exc_type, exc_val, exc_tb)))

logger.info("Inicializando o App Sprint Dashboard...")

# ==========================================
# 1. CONFIGURAÇÕES DA PÁGINA E UI/UX
# ==========================================
st.set_page_config(page_title="Sprint Dashboard", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
    .stApp { background-color: #F8FAFC; }
    .metric-card { background-color: #ffffff; border-radius: 12px; padding: 20px; border: 1px solid #E2E8F0; box-shadow: 0 1px 3px rgba(0,0,0,0.05); margin-bottom: 1rem; }
    .metric-title { color: #64748B; font-size: 0.85rem; font-weight: 600; text-transform: uppercase; margin-bottom: 8px;}
    .metric-value { font-size: 2rem; font-weight: 800; line-height: 1; }
    .metric-sub { color: #94A3B8; font-size: 0.85rem; margin-top: 8px; font-weight: 500;}
    .badge { padding: 4px 12px; border-radius: 9999px; font-size: 0.75rem; font-weight: 700; display: inline-block; }
    .bg-done { background-color: #DEF7EC; color: #03543F; border: 1px solid #31C48D;} 
    .bg-review { background-color: #FEF08A; color: #713F12; border: 1px solid #FDE047;} 
    .bg-progress { background-color: #E1EFFE; color: #1E429F; border: 1px solid #3F83F8;} 
    .bg-pending { background-color: #F1F5F9; color: #475569; border: 1px solid #CBD5E1;} 
    h1, h2, h3 { color: #0F172A; font-weight: 700 !important; }
</style>
""", unsafe_allow_html=True)

def render_metric(title, value, sub, color="#0F172A"):
    st.markdown(f'<div class="metric-card"><div class="metric-title">{title}</div><div class="metric-value" style="color: {color};">{value}</div><div class="metric-sub">{sub}</div></div>', unsafe_allow_html=True)

def render_badge(status):
    cores = {"Concluída": "bg-done", "Em Análise": "bg-review", "Em Andamento": "bg-progress", "Pendente": "bg-pending"}
    return f'<span class="badge {cores.get(status, "bg-pending")}">{status}</span>'

# ==========================================
# 2. VARIÁVEIS E CONFIGURAÇÕES DE NEGÓCIO
# ==========================================
with TraceOperation("load_config"):
    TOKEN = os.getenv("TOKEN")
    EMAIL = os.getenv("EMAIL")
    JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
    BOARD_ID = os.getenv("BOARD_ID")
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

    auth = HTTPBasicAuth(EMAIL, TOKEN) if EMAIL and TOKEN else None
    HEADERS = {"Accept": "application/json"}

    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception:
        config = {}

    GITHUB_ORG = config.get("github", {}).get("org", "")
    GITHUB_REPOS = config.get("github", {}).get("repos", [])
    MAPEAMENTO_DEV = config.get("dev_mapping", {})
    
    # Busca dinâmica: se não achar no json, usa o padrão. Tudo é convertido para minúsculo para evitar erros.
    TIPOS_CONTEXTO = [t.lower() for t in config.get("tipos_contexto", ['epic', 'épico', 'story', 'história', 'user story'])]

def normalizar_usuario(nome_bruto):
    if not nome_bruto or pd.isna(nome_bruto): return "nenhum"
    nome_limpo = str(nome_bruto).lower().strip().replace(" ", "-")
    return MAPEAMENTO_DEV.get(nome_limpo, nome_limpo)

def extrair_chave_jira(texto):
    """Extrai o padrão nativo universal do Jira (LETRAS-NUMEROS) de qualquer texto."""
    if not texto: return None
    match = re.search(r'([A-Za-z]+-\d+)', texto)
    return match.group(1).upper() if match else None

def classificar_status(status_raw):
    """Mapeamento inteligente e dinâmico de nomenclatura de status."""
    status_lower = status_raw.lower()
    if any(x in status_lower for x in ['concluíd', 'done', 'finaliz', 'fechado', 'resolvido', 'entregue']): return "Concluída"
    if any(x in status_lower for x in ['review', 'análise', 'analise', 'qa', 'teste', 'homologação']): return "Em Análise"
    if any(x in status_lower for x in ['andamento', 'progress', 'doing', 'execução']): return "Em Andamento"
    return "Pendente"

# ==========================================
# 3. EXTRATORES DE DADOS (PERFORMÁTICOS O(1))
# ==========================================
@st.cache_data(ttl=3600)
def process_jira_data():
    logger.info("Iniciando extração Jira O(1)...")
    if not auth: return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    
    url_board = f"https://{JIRA_DOMAIN}/rest/agile/1.0/board/{BOARD_ID}/issue"
    todas_issues = []
    
    with TraceOperation("jira_fetch_all_issues", board=BOARD_ID) as op:
        start_at, max_results = 0, 100
        while True:
            params = {"maxResults": max_results, "startAt": start_at, "expand": "changelog,worklog"}
            res = requests.get(url_board, auth=auth, headers=HEADERS, params=params)
            
            if res.status_code == 200:
                issues_page = res.json().get("issues", [])
                todas_issues.extend(issues_page)
                if len(issues_page) < max_results: break
                start_at += max_results
            else:
                handle_http_error(res, "Jira", op.op_id)
                break

    atividades, contextos, worklogs, changelogs = [], {}, [], []

    with TraceOperation("jira_classify_issues"):
        for iss in todas_issues:
            key = iss.get("key")
            fields = iss.get("fields", {})
            tipo_raw = fields.get("issuetype", {}).get("name", "Task")
            tipo_lower = tipo_raw.lower()
            summary = fields.get("summary", "")
            
            sprint_info = fields.get("sprint")
            sprint_name, sprint_start, sprint_end = "Backlog", "", ""
            if isinstance(sprint_info, dict):
                sprint_name = sprint_info.get("name", "Backlog")
                sprint_start = sprint_info.get("startDate", "")
                sprint_end = sprint_info.get("endDate", "")

            # 1. Classifica Contexto
            if tipo_lower in TIPOS_CONTEXTO:
                contextos[key] = summary
                continue

            # 2. Paternidade
            parent_key = fields["parent"].get("key") if fields.get("parent") else fields.get("customfield_10014")

            # 3. Classificação Dinâmica da Atividade
            status_group = classificar_status(fields.get("status", {}).get("name", ""))
            
            # Identificação de Bugs para badge visual
            prefixo_bug = "🐛 " if "bug" in tipo_lower or "defect" in tipo_lower or "erro" in tipo_lower else ""
            
            assignee_data = fields.get("assignee")
            assignee_raw = assignee_data.get("displayName") if assignee_data else "Nenhum"
            time_spent_h = (fields.get("timespent", 0) or 0) / 3600
            labels = fields.get("labels", [])

            atividades.append({
                "issue_key": key,
                "summary": f"{prefixo_bug}{summary}",
                "type": tipo_raw,
                "status_group": status_group,
                "assignee": normalizar_usuario(assignee_raw),
                "time_spent_h": time_spent_h,
                "label": labels[0] if labels else "Geral",
                "sprint": sprint_name,
                "sprint_start": sprint_start,
                "sprint_end": sprint_end,
                "parent_key": parent_key
            })

            # 4. Extração O(1) de Worklogs nativos na resposta
            for w in (fields.get("worklog") or {}).get("worklogs", []):
                author_data = w.get("author")
                worklogs.append({
                    "issue_key": key, 
                    "author": normalizar_usuario(author_data.get("displayName", "") if author_data else ""),
                    "date": w.get("started", "")[:10], 
                    "time_spent_h": w.get("timeSpentSeconds", 0) / 3600
                })
                
            # 5. Extração O(1) de Changelogs nativos na resposta
            for h in (iss.get("changelog") or {}).get("histories", []):
                date = h.get("created", "")[:10]
                author_data = h.get("author")
                author_log = normalizar_usuario(author_data.get("displayName", "") if author_data else "")
                
                for item in h.get("items", []):
                    if item.get("field") == "status":
                        changelogs.append({
                            "issue_key": key, "author": author_log, "date": date, 
                            "from_status": classificar_status(str(item.get("fromString"))), 
                            "to_status": classificar_status(str(item.get("toString")))
                        })

    df_activities = pd.DataFrame(atividades)
    df_context = pd.DataFrame(list(contextos.items()), columns=['parent_key', 'parent_summary'])
    df_worklogs = pd.DataFrame(worklogs)
    df_changelogs = pd.DataFrame(changelogs)
    
    if not df_activities.empty and not df_context.empty:
        df_activities = df_activities.merge(df_context, on='parent_key', how='left')
        df_activities['parent_summary'] = df_activities['parent_summary'].fillna("Tarefa Independente")
    elif not df_activities.empty:
        df_activities['parent_summary'] = "Tarefa Independente"

    if not df_worklogs.empty: df_worklogs['date'] = pd.to_datetime(df_worklogs['date'])
    if not df_changelogs.empty: df_changelogs['date'] = pd.to_datetime(df_changelogs['date'])

    return df_activities, df_worklogs, df_changelogs

@st.cache_data(ttl=3600)
def fetch_github_commits():
    logger.info("Iniciando extração GitHub...")
    if not GITHUB_TOKEN: return pd.DataFrame()
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    all_commits = []

    for repo in GITHUB_REPOS:
        with TraceOperation(f"github_fetch_{repo}") as op:
            try:
                res_branches = requests.get(f"https://api.github.com/repos/{GITHUB_ORG}/{repo}/branches", headers=headers, timeout=15)
                if res_branches.status_code != 200: 
                    handle_http_error(res_branches, "GitHub Branches", op.op_id)
                    continue

                for branch in res_branches.json():
                    branch_name = branch['name']
                    res_commits = requests.get(f"https://api.github.com/repos/{GITHUB_ORG}/{repo}/commits?sha={branch_name}&per_page=100", headers=headers)
                    if res_commits.status_code == 200:
                        for c in res_commits.json():
                            msg = c.get("commit", {}).get("message", "")
                            sha = c.get("sha")
                            author = c.get("author", {}).get("login") or c.get("commit", {}).get("author", {}).get("name", "")
                            
                            all_commits.append({
                                "sha": sha, "url": f"https://github.com/{GITHUB_ORG}/{repo}/commit/{sha}", "repo": repo,
                                "author": normalizar_usuario(author), "date": c.get("commit", {}).get("author", {}).get("date", "")[:10],
                                "message": msg, "issue_key": extrair_chave_jira(msg) 
                            })
                    else:
                        handle_http_error(res_commits, "GitHub Commits", op.op_id)
            except Exception as e:
                logger.error(f"[OpID:{op.op_id}] Exception GitHub: {e}")
                continue
                
    df_commits = pd.DataFrame(all_commits)
    if not df_commits.empty: df_commits.drop_duplicates(subset=['sha'], inplace=True)
    return df_commits

# ==========================================
# 4. MODAL DE DETALHES
# ==========================================
@st.dialog("🔍 Detalhes da Atividade", width="large")
def modal_detalhes_tarefa(activity, df_all_commits, dict_retornos):
    st.subheader(f"{activity['issue_key']}: {activity['summary']}")
    
    if activity['parent_summary'] != "Tarefa Independente":
        st.caption(f"Contexto/História Pai: {activity.get('parent_key', '')} - {activity['parent_summary']}")
    else:
        st.caption("Contexto: Tarefa Independente da Sprint")
        
    badge_html = render_badge(activity['status_group'])
    qtd_voltou = dict_retornos.get(activity['issue_key'], 0)
    
    if activity.get('is_late', False):
        st.markdown(f"<div style='background-color: #FEF2F2; border-left: 4px solid #EF4444; padding: 12px; margin-bottom: 16px; border-radius: 6px; color: #991B1B; font-size: 0.85rem;'><b>⏰ Entrega com Atraso:</b> Esta tarefa foi concluída após a data oficial de término da Sprint.</div>", unsafe_allow_html=True)
        
    if qtd_voltou > 0:
        st.markdown(f"<div style='background-color: #FFFBEB; border-left: 4px solid #F59E0B; padding: 12px; margin-bottom: 16px; border-radius: 6px; color: #92400E; font-size: 0.85rem;'><b>⚠️ Atenção:</b> Esta tarefa já retornou <b>{qtd_voltou} vezes</b> da etapa de Code Review.</div>", unsafe_allow_html=True)

    col_info, col_commits = st.columns([1, 1.5])
    
    with col_info:
        st.markdown(f"{badge_html}", unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        st.write(f"**Responsável:** 👨‍💻 {activity['assignee']}")
        st.write(f"**Esforço Registrado:** ⏳ {activity['time_spent_h']:.1f} horas")
        st.write(f"**Tipo:** `{activity['type']}` | **Label:** `{activity['label']}`")
        st.write(f"**Jira:** [Visualizar Ticket](https://{JIRA_DOMAIN}/browse/{activity['issue_key']})")

    with col_commits:
        st.markdown("**🔗 Código e Commits Vinculados (GitHub):**")
        commits_vinc = df_all_commits[df_all_commits['issue_key'] == activity['issue_key']] if not df_all_commits.empty else pd.DataFrame()
        
        if not commits_vinc.empty:
            commits_do_dev = commits_vinc[commits_vinc['author'] == activity['assignee']]
            outros_commits = commits_vinc[commits_vinc['author'] != activity['assignee']]
            
            if not commits_do_dev.empty:
                st.markdown(f"✅ **{activity['assignee']}:**")
                for _, c in commits_do_dev.iterrows():
                    st.markdown(f"- 🕒 {c['date']} | [{c['message'][:45]}...]({c['url']})")
                    
            if not outros_commits.empty:
                st.markdown(f"🔄 **Outros membros:**")
                for _, c in outros_commits.iterrows():
                    st.markdown(f"- 🕒 {c['date']} | 👤 *{c['author']}* | [{c['message'][:45]}...]({c['url']})")
        else:
            st.caption("👻 Sem commits mapeados nesta atividade.")

# ==========================================
# 5. DASHBOARD PRINCIPAL E SIDEBAR
# ==========================================
col_logo, col_btn = st.sidebar.columns([3, 1])
with col_logo: st.image("https://cdn-icons-png.flaticon.com/512/732/732214.png", width=40) 
with col_btn:
    if st.button("🔄", help="Limpar cache"):
        process_jira_data.clear()
        fetch_github_commits.clear()
        st.rerun()

st.sidebar.header("🎛️ Filtros do Dashboard")
st.sidebar.markdown("---")
st.title("🎯 Sprint Dashboard")
st.markdown("Acompanhe o progresso das metas, esforço do time e commits integrados de forma simples e visual.")

with st.spinner('Sincronizando Dados (Processamento O(1))...'):
    df_activities, df_worklogs, df_changelogs = process_jira_data()
    df_commits = fetch_github_commits()

if df_activities.empty:
    st.error("Nenhuma atividade executável encontrada. Verifique os logs de conexão do Jira na sua IDE.")
    st.stop()

sprints_disponiveis = ["Todas"] + df_activities['sprint'].dropna().unique().tolist()
sprint_selecionada = st.sidebar.selectbox("1. Filtrar por Sprint", sprints_disponiveis)

if sprint_selecionada != "Todas":
    df_sprint = df_activities[df_activities['sprint'] == sprint_selecionada].copy()
else:
    df_sprint = df_activities.copy()

if df_sprint.empty:
    st.warning("Esta Sprint não possui atividades executáveis.")
    st.stop()

# ==========================================
# LÓGICA DE IDENTIFICAÇÃO DE ATRASO
# ==========================================
df_sprint['is_late'] = False
if not df_changelogs.empty:
    entregas_historico = df_changelogs[df_changelogs['to_status'] == 'Concluída']
    if not entregas_historico.empty:
        ultimas_entregas = entregas_historico.groupby('issue_key')['date'].max().reset_index()
        df_sprint = df_sprint.merge(ultimas_entregas, on='issue_key', how='left').rename(columns={'date': 'completion_date'})
        
        def checar_atraso(row):
            if row['status_group'] == 'Concluída' and pd.notna(row['completion_date']) and pd.notna(row['sprint_end']) and row['sprint_end'] != "":
                try:
                    sprint_end_date = pd.to_datetime(str(row['sprint_end'])[:10])
                    if row['completion_date'] > sprint_end_date:
                        return True
                except:
                    pass
            return False

        df_sprint['is_late'] = df_sprint.apply(checar_atraso, axis=1)

dict_retornos = {}
if not df_changelogs.empty:
    df_retornos = df_changelogs[(df_changelogs['from_status'] == 'Em Análise') & (df_changelogs['to_status'] == 'Em Andamento')]
    dict_retornos = df_retornos.groupby('issue_key').size().to_dict()

# --- SIDEBAR: ATIVIDADES DO DEV ---
st.sidebar.markdown("---")
dev_selecionado = st.sidebar.selectbox("2. Visão do Desenvolvedor", ["Todos"] + df_sprint['assignee'].unique().tolist())

if dev_selecionado != "Todos":
    df_sprint_dev = df_sprint[df_sprint['assignee'] == dev_selecionado]
    st.sidebar.markdown("---")
    st.sidebar.subheader(f"👤 Últimos Registros: {dev_selecionado}")
    
    st.sidebar.markdown("**Última ação no Jira:**")
    wl_dev = df_worklogs[df_worklogs['author'] == dev_selecionado] if not df_worklogs.empty else pd.DataFrame()
    cl_dev = df_changelogs[df_changelogs['author'] == dev_selecionado] if not df_changelogs.empty else pd.DataFrame()
    
    ultimo_wl = wl_dev.sort_values(by='date', ascending=False).iloc[0] if not wl_dev.empty else None
    ultimo_cl = cl_dev.sort_values(by='date', ascending=False).iloc[0] if not cl_dev.empty else None
    
    if ultimo_wl is not None:
        st.sidebar.markdown(f"**Worklog ({ultimo_wl['date'].strftime('%d/%m')}):**<br>Lançou {ultimo_wl['time_spent_h']:.1f}h em **{ultimo_wl['issue_key']}**", unsafe_allow_html=True)
        row_tarefa = df_activities[df_activities['issue_key'] == ultimo_wl['issue_key']]
        if not row_tarefa.empty and st.sidebar.button(f"Abrir 📖 {ultimo_wl['issue_key']}", key="btn_wl", use_container_width=True):
            modal_detalhes_tarefa(row_tarefa.iloc[0], df_commits, dict_retornos)

    if ultimo_cl is not None:
        st.sidebar.markdown("<br>", unsafe_allow_html=True)
        st.sidebar.markdown(f"**Status ({ultimo_cl['date'].strftime('%d/%m')}):**<br>Moveu **{ultimo_cl['issue_key']}** para *{ultimo_cl['to_status']}*", unsafe_allow_html=True)
        row_tarefa = df_activities[df_activities['issue_key'] == ultimo_cl['issue_key']]
        if not row_tarefa.empty and st.sidebar.button(f"Abrir 📖 {ultimo_cl['issue_key']}", key="btn_cl", use_container_width=True):
            modal_detalhes_tarefa(row_tarefa.iloc[0], df_commits, dict_retornos)

    if ultimo_wl is None and ultimo_cl is None: st.sidebar.caption("Sem ações recentes no Jira.")

    st.sidebar.markdown("**Últimos Commits no GitHub (Em suas tarefas):**")
    if not df_commits.empty:
        chaves_do_dev = df_sprint_dev['issue_key'].tolist()
        commits_dev = df_commits[(df_commits['author'] == dev_selecionado) | (df_commits['issue_key'].isin(chaves_do_dev))].sort_values(by='date', ascending=False).head(3)
        if not commits_dev.empty:
            for _, c in commits_dev.iterrows():
                st.sidebar.markdown(f"- 🕒 {c['date']} | [{c['message'][:35]}...]({c['url']})")
        else:
            st.sidebar.caption("Sem commits mapeados.")

    df_sprint = df_sprint_dev

# ==========================================
# 6. MÉTRICAS EXECUTIVAS
# ==========================================
total_act = len(df_sprint)
qtd_concluida = len(df_sprint[df_sprint['status_group'] == 'Concluída'])
qtd_andamento = len(df_sprint[df_sprint['status_group'] == 'Em Andamento'])
qtd_analise = len(df_sprint[df_sprint['status_group'] == 'Em Análise'])
qtd_pendente = len(df_sprint[df_sprint['status_group'] == 'Pendente'])

esf_total = df_sprint['time_spent_h'].sum()
esf_concluida = df_sprint[df_sprint['status_group'] == 'Concluída']['time_spent_h'].sum()
esf_analise = df_sprint[df_sprint['status_group'] == 'Em Análise']['time_spent_h'].sum()
esf_andamento = df_sprint[df_sprint['status_group'] == 'Em Andamento']['time_spent_h'].sum()

st.markdown(f"<h3 style='margin-bottom: 20px;'>📊 Resumo Executivo: <span style='color: #3B82F6;'>{sprint_selecionada}</span></h3>", unsafe_allow_html=True)

def render_task_list(df_subset, prefix_key):
    if df_subset.empty:
        st.caption("Nenhuma tarefa.")
    else:
        for _, row in df_subset.iterrows():
            aviso_atraso = " <span style='color: #EF4444; font-size: 0.75rem; font-weight: 700;'>⏰ Fora do prazo</span>" if row.get('is_late', False) else ""
            st.markdown(f"<div style='font-size: 0.85rem; line-height: 1.4; margin-bottom: 4px;'><b>👨‍💻 {row['assignee']}</b> | ⏳ {row['time_spent_h']:.1f}h{aviso_atraso}<br><span style='color: #3B82F6; font-weight: 600;'>{row['issue_key']}</span>: <span style='color: #475569;'>{row['summary']}</span><br><span style='color: #94A3B8; font-size: 0.75rem;'>Pai: {row['parent_summary']}</span></div>", unsafe_allow_html=True)
            if st.button(f"Detalhes 🔍", key=f"btn_{prefix_key}_{row['issue_key']}", use_container_width=True):
                modal_detalhes_tarefa(row, df_commits, dict_retornos)
            st.markdown("<div style='border-bottom: 1px solid #E2E8F0; margin-bottom: 12px; margin-top: 4px;'></div>", unsafe_allow_html=True)

col1, col2, col3, col4, col5 = st.columns(5)
with col1: 
    render_metric("Total da Meta", f"{total_act} Tasks", f"{esf_total:.1f}h de Esforço", color="#0F172A")
    with st.expander("Ver tarefas"): render_task_list(df_sprint, "todas")
with col2: 
    render_metric("Concluídas", f"{qtd_concluida}", f"{esf_concluida:.1f}h de Esforço", color="#059669")
    with st.expander("Ver tarefas"): render_task_list(df_sprint[df_sprint['status_group'] == 'Concluída'], "concluida")
with col3: 
    render_metric("Em Análise", f"{qtd_analise}", f"{esf_analise:.1f}h de Esforço", color="#D97706")
    with st.expander("Ver tarefas"): render_task_list(df_sprint[df_sprint['status_group'] == 'Em Análise'], "analise")
with col4: 
    render_metric("Andamento", f"{qtd_andamento}", f"{esf_andamento:.1f}h de Esforço", color="#2563EB")
    with st.expander("Ver tarefas"): render_task_list(df_sprint[df_sprint['status_group'] == 'Em Andamento'], "andamento")
with col5: 
    render_metric("Pendentes", f"{qtd_pendente}", "Aguardando início", color="#64748B")
    with st.expander("Ver tarefas"): render_task_list(df_sprint[df_sprint['status_group'] == 'Pendente'], "pendente")

# ==========================================
# 7. BURNDOWN CHART CENTRALIZADO (ATUALIZADO PARA ATRASOS)
# ==========================================
st.markdown("---")
with st.expander("📉 Burndown Chart de Atividades da Sprint", expanded=True):
    try:
        datas_list = []
        if not df_worklogs.empty and 'date' in df_worklogs.columns:
            datas_list.extend(df_worklogs['date'].dropna().tolist())
        if not df_changelogs.empty and 'date' in df_changelogs.columns:
            datas_list.extend(df_changelogs['date'].dropna().tolist())
            
        todas_datas = pd.to_datetime(datas_list) if datas_list else pd.Series(dtype='datetime64[ns]')
        
        s_start = df_sprint['sprint_start'].replace('', pd.NA).dropna().max()
        s_end = df_sprint['sprint_end'].replace('', pd.NA).dropna().max()
        
        # Início e Fim oficiais da Sprint
        data_min = pd.to_datetime(str(s_start)[:10]) if pd.notna(s_start) else (todas_datas.min() if not todas_datas.empty else pd.to_datetime('today') - pd.Timedelta(days=7))
        sprint_end_date = pd.to_datetime(str(s_end)[:10]) if pd.notna(s_end) else pd.to_datetime('today') + pd.Timedelta(days=1)
        
        entregas_bd = df_changelogs[(df_changelogs['issue_key'].isin(df_sprint['issue_key'])) & (df_changelogs['to_status'] == 'Concluída')] if not df_changelogs.empty else pd.DataFrame()
        
        # O gráfico de Burndown precisa ir até hoje se a sprint estiver atrasada, ou até a última entrega se todas já foram entregues com atraso.
        hoje = pd.Timestamp.today().normalize()
        ultima_entrega = entregas_bd['date'].max() if not entregas_bd.empty else sprint_end_date
        
        data_max = max(sprint_end_date, ultima_entrega, hoje)
        if data_min > data_max: data_max = data_min + pd.Timedelta(days=1)
            
        dias_sprint = pd.date_range(start=data_min, end=data_max)
        df_bd = pd.DataFrame({'data': dias_sprint})
        
        if not entregas_bd.empty:
            df_primeiras = entregas_bd.groupby('issue_key')['date'].min().reset_index()
            entregas_dia = df_primeiras.groupby('date').size().reset_index(name='concluidas')
            entregas_dia['date'] = pd.to_datetime(entregas_dia['date'])
            df_bd = df_bd.merge(entregas_dia, left_on='data', right_on='date', how='left').fillna(0)
            df_bd['cumulativo'] = df_bd['concluidas'].cumsum()
        else:
            df_bd['cumulativo'] = 0
            
        total_tasks_meta = len(df_sprint)
        df_bd['restantes'] = total_tasks_meta - df_bd['cumulativo']
        
        # CÁLCULO DA LINHA IDEAL (Deve zerar exatamente no dia do Fim Oficial da Sprint)
        dias_oficiais = (sprint_end_date - data_min).days
        passo = total_tasks_meta / dias_oficiais if dias_oficiais > 0 else total_tasks_meta
        
        def calc_ideal(data_atual):
            dias_passados = (data_atual - data_min).days
            return max(0, total_tasks_meta - (dias_passados * passo))
            
        df_bd['ideal'] = df_bd['data'].apply(calc_ideal)
        df_real = df_bd[df_bd['data'] <= hoje]
        
        fig_bd = go.Figure()
        fig_bd.add_trace(go.Scatter(x=df_bd['data'], y=df_bd['ideal'], mode='lines', name='Linha Ideal', line=dict(dash='dash', color='gray')))
        fig_bd.add_trace(go.Scatter(x=df_real['data'], y=df_real['restantes'], mode='lines+markers', name='Atividades Restantes', line=dict(color='#EF4444', width=3)))
        
        fig_bd.update_layout(xaxis=dict(range=[data_min, data_max]), xaxis_title="Dias da Sprint", yaxis_title="Quantidade de Atividades", hovermode="x unified", legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        
        if pd.notna(s_end) and s_end != "":
            fig_bd.add_vline(x=sprint_end_date, line_dash="dash", line_color="green")
            fig_bd.add_annotation(x=sprint_end_date, y=max(df_bd['restantes'])*1.05, text="Fim Oficial", showarrow=False, font=dict(color="green", size=12), xanchor="left")
            
            # Destacar visualmente o período de atraso se o gráfico estourar o prazo
            if data_max > sprint_end_date:
                fig_bd.add_vrect(
                    x0=sprint_end_date, x1=data_max,
                    fillcolor="#FEF2F2", opacity=0.5,
                    layer="below", line_width=0,
                    annotation_text="Zona de Atraso", annotation_position="top right",
                    annotation_font_size=10, annotation_font_color="#EF4444"
                )
            
        st.plotly_chart(fig_bd, use_container_width=True)
    except Exception as e:
        st.error(f"Erro ao gerar gráfico de Burndown: {e}")

st.markdown("<br>", unsafe_allow_html=True)
st.subheader("📈 Métricas de Média por Tag/Categoria")
df_meta_concluidas = df_sprint[df_sprint['status_group'] == 'Concluída']
if not df_meta_concluidas.empty:
    media_geral = df_meta_concluidas['time_spent_h'].mean()
    st.caption(f"**Média geral da equipe:** {media_geral:.2f}h por atividade concluída.")

    df_media_tag = df_meta_concluidas.groupby('label')['time_spent_h'].mean().reset_index().sort_values(by='time_spent_h', ascending=False)
    fig_tags = px.bar(df_media_tag, x='label', y='time_spent_h', text_auto='.1f', color_discrete_sequence=['#6366F1'], labels={'label': 'Categoria (TAG)', 'time_spent_h': 'Média de Horas'})
    fig_tags.update_layout(plot_bgcolor="white", xaxis_title="", yaxis_title="Média (Horas)")
    st.plotly_chart(fig_tags, use_container_width=True)
else:
    st.info("Ainda não há tarefas concluídas nesta meta para gerar as métricas de média.")

# ==========================================
# 8. EXECUÇÃO AGRUPADA
# ==========================================
st.markdown("---")
st.subheader("📋 Execução: Agrupamento por Contexto (Histórias/Épicos)")

grupos_contexto = df_sprint.groupby('parent_summary')
for nome_contexto, atividades_grupo in grupos_contexto:
    icone_header = "📖" if nome_contexto != "Tarefa Independente" else "⚡"
    
    with st.expander(f"{icone_header} {nome_contexto} — ({len(atividades_grupo)} atividades)", expanded=False):
        for _, act in atividades_grupo.sort_values(by='status_group').iterrows():
            badge_status = render_badge(act['status_group'])
            voltas = dict_retornos.get(act['issue_key'], 0)
            alerta_review = f" | ⚠️ Voltou {voltas}x" if voltas > 0 else ""
            alerta_atraso = " | ⏰ Atrasada" if act.get('is_late', False) else ""
            
            commits_vinc = df_commits[df_commits['issue_key'] == act['issue_key']] if not df_commits.empty else []
            info_commit = "👻 Sem Commit" if len(commits_vinc) == 0 else f"🔗 {len(commits_vinc)} Commits"
            
            col_act, col_btn = st.columns([6, 1])
            with col_act:
                st.markdown(f"**{act['issue_key']}** [{act['type']}] - {act['summary']} <br> <small>👨‍💻 {act['assignee']} | ⏳ {act['time_spent_h']:.1f}h | {info_commit}{alerta_review}{alerta_atraso}</small> {badge_status}", unsafe_allow_html=True)
            with col_btn:
                if st.button("🔍 Detalhes", key=f"btn_grp_{act['issue_key']}"):
                    modal_detalhes_tarefa(act, df_commits, dict_retornos)
            st.divider()

# ==========================================
# 9. GRÁFICO DO DEV
# ==========================================
if dev_selecionado != "Todos":
    st.markdown("---")
    st.subheader(f"📈 Gráfico de Esforço Diário: {dev_selecionado}")
    mostrar_estrelas = st.toggle("⭐ Mostrar marcações de entregas no gráfico", value=True)
    df_dev_wl = df_worklogs[df_worklogs['author'] == dev_selecionado] if not df_worklogs.empty else pd.DataFrame()
    
    if not df_dev_wl.empty:
        df_diario = df_dev_wl.groupby('date')['time_spent_h'].sum().reset_index()
        fig_linha = go.Figure(go.Scatter(x=df_diario['date'], y=df_diario['time_spent_h'], mode='lines+markers', line=dict(color='#3B82F6', width=3)))
        
        atividades_entregues = df_sprint[df_sprint['status_group'] == 'Concluída']['issue_key'].tolist()
        if mostrar_estrelas and not df_changelogs.empty and atividades_entregues:
            entregas_historico = df_changelogs[(df_changelogs['issue_key'].isin(atividades_entregues)) & (df_changelogs['to_status'] == 'Concluída')]
            if not entregas_historico.empty:
                entregas_dia = entregas_historico.groupby('issue_key')['date'].max().reset_index().groupby('date').size().reset_index(name='qtd')
                for _, row in entregas_dia.iterrows():
                    dt_data = pd.to_datetime(row['date'])
                    y_val = df_diario.loc[df_diario['date'] == dt_data, 'time_spent_h'].values[0] if dt_data in df_diario['date'].values else 0
                    fig_linha.add_trace(go.Scatter(x=[dt_data], y=[y_val], mode='markers+text', marker=dict(color='#10B981', size=16, symbol='star'), text=[f"⭐ {row['qtd']}"], textposition='top center', showlegend=False))
                    
        fig_linha.update_layout(plot_bgcolor="white", xaxis_title="Dias", yaxis_title="Horas Trabalhadas", hovermode="x unified")
        fig_linha.update_yaxes(rangemode="tozero")
        st.plotly_chart(fig_linha, use_container_width=True)
    else:
        st.info("Nenhuma hora registrada para este desenvolvedor.")

# ==========================================
# 10. DEBUG DE MAPEAMENTO
# ==========================================
st.markdown("---")
with st.expander("🧪 Debug de Mapeamento Relacional (Logs)"):
    col_dbg1, col_dbg2 = st.columns(2)
    with col_dbg1:
        st.markdown("**Atividades Mapeadas (Jira)**")
        if not df_activities.empty: st.dataframe(df_activities[['issue_key', 'summary', 'assignee', 'type', 'status_group']], use_container_width=True)
    with col_dbg2:
        st.markdown("**Commits Mapeados (GitHub)**")
        if not df_commits.empty: st.dataframe(df_commits[['message', 'issue_key', 'author']].dropna(subset=['issue_key']), use_container_width=True)

logger.info(f"Fim do fluxo (RunID: {RUN_ID})")