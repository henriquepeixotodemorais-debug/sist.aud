import os
import io
import base64
import requests
import streamlit as st
import pandas as pd
from cryptography.fernet import Fernet, InvalidToken

# ---------------------------------------------------------
# CONFIGURAÇÃO DO TEMA
# ---------------------------------------------------------
st.set_page_config(layout="wide", initial_sidebar_state="expanded")
st.markdown("""
    <style>
        :root { color-scheme: light; }
    </style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------
# SECRETS E VARIÁVEIS DO GITHUB
# ---------------------------------------------------------
# Use st.secrets no Streamlit Cloud; fallback para variáveis de ambiente
GITHUB_TOKEN = st.secrets.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
GITHUB_USER = st.secrets.get("GITHUB_USER") or os.environ.get("GITHUB_USER")
GITHUB_REPO = st.secrets.get("GITHUB_REPO") or os.environ.get("GITHUB_REPO")
ENCRYPTION_KEY = st.secrets.get("ENCRYPTION_KEY") or os.environ.get("ENCRYPTION_KEY")
SENHA_AUTORIDADES = st.secrets.get("SENHA_AUTORIDADES") or os.environ.get("SENHA_AUTORIDADES")  
SENHA_BASE = st.secrets.get("SENHA_BASE") or os.environ.get("SENHA_BASE")  
SENHA_SECRETARIOS = st.secrets.get("SENHA_SECRETARIOS") or os.environ.get("SENHA_SECRETARIOS")  
# Nome do arquivo no repositório. Usar extensão .enc deixa claro que está cifrado.
GITHUB_FILE = "baseaud.csv.enc"

# Endpoints (API_URL usado para criar/atualizar; RAW_URL opcional para download direto)
API_URL = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{GITHUB_FILE}" if GITHUB_USER and GITHUB_REPO else None
RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/{GITHUB_FILE}" if GITHUB_USER and GITHUB_REPO else None

# ---------------------------------------------------------
# VALIDAÇÃO INICIAL E FERNET
# ---------------------------------------------------------
missing = []
if not GITHUB_USER:
    missing.append("GITHUB_USER")
if not GITHUB_REPO:
    missing.append("GITHUB_REPO")
if not ENCRYPTION_KEY:
    missing.append("ENCRYPTION_KEY")

if missing:
    st.error(
        "Faltam configurações: " + ", ".join(missing) + ".\n"
        "No Streamlit Cloud adicione ENCRYPTION_KEY, GITHUB_TOKEN, GITHUB_USER e GITHUB_REPO em Secrets."
    )
    st.stop()

# Inicializa Fernet (espera-se chave gerada por Fernet.generate_key().decode())
try:
    fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)
except Exception:
    st.error("Chave de criptografia inválida. Gere com Fernet.generate_key() e cole em ENCRYPTION_KEY (ex.: 'g6K8...==').")
    st.stop()

# ---------------------------------------------------------
# LIMPA CACHE AO DIGITAR QUALQUER SENHA
# ---------------------------------------------------------
password = st.text_input("Insira a chave de acesso", type="password")
if password:
    st.cache_data.clear()

# ---------------------------------------------------------
# COLUNAS ESPERADAS (caso o repositório comece vazio)
# ---------------------------------------------------------
EXPECTED_COLUMNS = [
    "data e horário",
    "sala de audiência",
    "número do processo relacionado",
    "parte a ser ouvida ou tipo de processo",
    "telefone da parte",
    "estado da intimação",
    "link do processo",
    "dimensão da audiência",
    "resumo dos fatos",
]

# ---------------------------------------------------------
# FUNÇÃO PARA CARREGAR CSV DO GITHUB (DESCRIPTOGRAFA NO CACHE)
# ---------------------------------------------------------
@st.cache_data(ttl=60)
def load_csv_from_github():
    """
    Baixa o arquivo via API (conteúdo em base64), decodifica e tenta descriptografar.
    Se 404 (arquivo não existe), retorna DataFrame vazio com colunas esperadas.
    Se descriptografia falhar, tenta interpretar como texto plano (compatibilidade).
    """
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

    # 1) Tenta obter metadados do arquivo via API
    r = requests.get(API_URL, headers=headers)
    if r.status_code == 404:
        # arquivo ainda não existe no repo
        return pd.DataFrame(columns=EXPECTED_COLUMNS)
    if r.status_code != 200:
        st.error(f"Erro ao acessar GitHub API: {r.status_code} {r.text}")
        st.stop()

    payload = r.json()
    content_b64 = payload.get("content", "")
    if not content_b64:
        st.error("Conteúdo vazio no GitHub.")
        st.stop()

    # remover quebras de linha e decodificar base64
    content_b64 = "".join(content_b64.splitlines())
    try:
        raw_bytes = base64.b64decode(content_b64)
    except Exception as e:
        st.error(f"Erro ao decodificar base64 do conteúdo: {e}")
        st.stop()

    # tenta descriptografar; se falhar, tenta interpretar como texto plano (fallback)
    try:
        plain_bytes = fernet.decrypt(raw_bytes)
        text = plain_bytes.decode("utf-8")
    except InvalidToken:
        # fallback: assume que raw_bytes é texto UTF-8 (arquivo legado não cifrado)
        try:
            text = raw_bytes.decode("utf-8")
        except Exception:
            st.error("Arquivo no GitHub não está cifrado com a chave fornecida e não é texto UTF-8.")
            st.stop()

    # Ler CSV aceitando , ou ;
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python")
    except Exception:
        try:
            df = pd.read_csv(io.StringIO(text), dtype=str, sep=",")
        except Exception:
            df = pd.read_csv(io.StringIO(text), dtype=str, sep=";")

    # garantir colunas esperadas (se faltar, adiciona vazias)
    for col in EXPECTED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    # manter apenas colunas esperadas na ordem correta
    df = df[EXPECTED_COLUMNS]

    return df.fillna("")

# ---------------------------------------------------------
# FUNÇÃO DE UPLOAD (CIFRA E ENVIA AO GITHUB) COM RETRY E LOG
# ---------------------------------------------------------
def upload_csv_to_github(uploaded_file):
    """
    Cifra os bytes do uploaded_file com Fernet, codifica em base64 e envia ao GitHub.
    Faz checagens: repo acessível, obtém default_branch, busca sha atual (se existir),
    tenta PUT e loga a resposta completa para diagnóstico.
    """
    if not GITHUB_TOKEN:
        st.error("GITHUB_TOKEN não configurado. Não é possível enviar ao GitHub.")
        return

    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    # 1) Verifica se o repo é acessível e obtém branch padrão
    repo_meta = requests.get(f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}", headers=headers)
    if repo_meta.status_code != 200:
        st.error("Repositório inacessível com esse token/owner/repo. Verifique GITHUB_USER, GITHUB_REPO e permissões do token.")
        return

    default_branch = repo_meta.json().get("default_branch", "main")

    # 2) Prepara conteúdo cifrado
    content = uploaded_file.getvalue()  # bytes do CSV
    encrypted = fernet.encrypt(content)
    encoded = base64.b64encode(encrypted).decode()

    # 3) Monta payload usando branch padrão
    api_url = API_URL
    payload = {
        "message": "Atualização automática do CSV (cifrado) via Streamlit",
        "content": encoded,
        "branch": default_branch
    }

    # 4) Tenta obter sha atual (se existir) e inclui no payload para update
    r_get = requests.get(api_url, headers=headers)
    if r_get.status_code == 200:
        sha = r_get.json().get("sha")
        payload["sha"] = sha

    # 5) Envia o arquivo
    put_response = requests.put(api_url, json=payload, headers=headers)

    # 6) Se conflito 409, tenta buscar sha de novo e reenviar
    if put_response.status_code == 409:
        new_r = requests.get(api_url, headers=headers)
        if new_r.status_code == 200:
            payload["sha"] = new_r.json().get("sha")
            put_response = requests.put(api_url, json=payload, headers=headers)

    if put_response.status_code in [200, 201]:
        st.success("CSV cifrado enviado com sucesso ao GitHub! Recarregando...")
        st.cache_data.clear()
        st.rerun()
    else:
        st.error(f"Erro ao enviar arquivo: {put_response.status_code} {put_response.text}")

# ---------------------------------------------------------
# MODO sisbase — UPLOAD DO CSV (CIFRADO)
# ---------------------------------------------------------
if password == SENHA_BASE:
    st.header("🗂 Painel de Administração da Base")
    st.info("Envie um CSV; ele será cifrado localmente e armazenado cifrado no GitHub (arquivo: " + GITHUB_FILE + ").")
    uploaded = st.file_uploader("📤 Enviar novo CSV (será cifrado)", type=["csv"])
    if uploaded:
        upload_csv_to_github(uploaded)
    st.stop()

    # ---------------------------------------------------------
# CARREGAR CSV DO GITHUB (DESCRIPTOGRAFA NO CACHE)
# ---------------------------------------------------------
df = load_csv_from_github()

# ---------------------------------------------------------
# PREPARAÇÃO DOS DADOS (MANTIDA COMO SOLICITADO)
# ---------------------------------------------------------
# Se o DataFrame estiver vazio (projeto começando sem base), mostra instrução
if df.empty:
    st.warning("Nenhuma base encontrada. Entre com a chave 'sisbase' e faça o upload do CSV para iniciar.")
    st.stop()

# df = df.sort_values(["dia", "sala de audiência", "data e horário"])

# ordena globalmente por data/hora e sala
# df = df.sort_values(["sala de audiência"], ascending=True).reset_index(drop=True)
# df = df.sort_values(["dia", "sala de audiência", "data e horário"])
# df = df.sort_values([ "dia", "data e horário","sala de audiência"])

df["data e horário"] = pd.to_datetime(df["data e horário"], dayfirst=True, errors="coerce") 
df["dia"] = df["data e horário"].dt.strftime("%d/%m/%y")
df = df.sort_values(["sala de audiência", "data e horário"])
# df = df.sort_values(["dia", "sala de audiência", "data e horário"])


# ---------------------------------------------------------
# FILTRO DE SALAS
# ---------------------------------------------------------
todas_salas = sorted(df["sala de audiência"].unique())

salas_selecionadas = st.multiselect(
    "Filtrar salas:",
    options=todas_salas,
    default=todas_salas,
)

if len(salas_selecionadas) == 0:
    st.warning("Selecione ao menos uma sala.")
    st.stop()

# ---------------------------------------------------------
# FUNÇÃO PARA MONTAR O BOX DE CADA PROCESSO
# ---------------------------------------------------------
def render_process_box(process_df, show_sensitive=False):
    row0 = process_df.iloc[0]

    with st.container():
        dt = row0["data e horário"]
        dt_str = dt.strftime("%d/%m/%Y %H:%M") if pd.notna(dt) else row0.get("data e horário", "")
        st.markdown(f"#### ⏰ {dt_str}")
        st.markdown(f"**Processo:** {row0.get('número do processo relacionado','')}")
        st.markdown(f"**Tipo:** {row0.get('parte a ser ouvida ou tipo de processo','')}")
        link = row0.get("link do processo", "")
        if link:
            # st.link_button(f"[🔗 Link do processo]",link)
            st.markdown(f"[🔗 Link da audiência]({link})")
        st.markdown(f"**Dimensão:** {row0.get('dimensão da audiência','')}")

        with st.expander("Resumo dos fatos"):
            st.write(row0.get("resumo dos fatos", ""))

        if show_sensitive:
            st.markdown("#### Partes:")
            for _, r in process_df.iloc[1:].iterrows():
                parte = r.get("parte a ser ouvida ou tipo de processo", "")
                telefone = r.get("telefone da parte", "")
                intimacao = r.get("estado da intimação", "")
                st.markdown(
                    f"""
                    <div style="margin-bottom:10px;">
                        <div style="font-weight:700; font-size:16px;">• {parte}</div>
                        <div style="margin-left:20px; font-size:14px; color:#444;">
                            Telefone: {telefone}<br>
                            Intimação: {intimacao}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

# ---------------------------------------------------------
# RENDERIZAÇÃO POR DIA E SALA
# ---------------------------------------------------------
def render_day(df_dia, show_sensitive):
    salas = [s for s in sorted(df_dia["sala de audiência"].unique()) if s in salas_selecionadas]
    if not salas:
        return
    cols = st.columns(len(salas))
    for idx, sala in enumerate(salas):
        with cols[idx]:
            st.markdown(f"## 🏛 Sala {sala}")
            df_sala = df_dia[df_dia["sala de audiência"] == sala]
            # st.metric(label="nº processos",value=df_sala.groupby("data e horário").size())
            # st.markdown(f"{df_sala.groupby('data e horário')['processos'].nunique()}")

            for processo, bloco in df_sala.groupby("data e horário"):
            # for processo, bloco in df_sala.groupby("número do processo relacionado"):
                render_process_box(bloco, show_sensitive)

# ---------------------------------------------------------
# SECRETÁRIOS
# ---------------------------------------------------------
if password == SENHA_SECRETARIOS:
    st.header("📌 Painel dos Secretários")
    for dia in df["dia"].unique():
        # df_dia = df[df["dia"] == dia]
        df_dia = df[df["dia"] == dia].sort_values(by="data e horário")
        if any(df_dia["sala de audiência"].isin(salas_selecionadas)):
            st.divider()
            st.markdown(f"# 📅 {dia}")
            render_day(df_dia, show_sensitive=True)

# ---------------------------------------------------------
# AUTORIDADES
# ---------------------------------------------------------
elif password == SENHA_AUTORIDADES:
    st.header("⚖ Painel das Autoridades - Audiências")
    for dia in df["dia"].unique():
        # df_dia = df[df["dia"] == dia]
        df_dia = df[df["dia"] == dia].sort_values(by="data e horário")
        if any(df_dia["sala de audiência"].isin(salas_selecionadas)):
            st.divider()
            st.markdown(f"# 📅 {dia}")
            render_day(df_dia, show_sensitive=False)

# ---------------------------------------------------------
# ACESSO NEGADO
# ---------------------------------------------------------
elif password.strip() != "":
    st.error("Chave inválida.")