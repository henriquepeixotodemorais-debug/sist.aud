import os
import streamlit as st
import pandas as pd
import requests
import base64
import io
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
# CONFIGURAÇÕES DO GITHUB E CHAVE (st.secrets ou env)
# ---------------------------------------------------------
# Usamos .get para evitar exceção imediata quando rodando localmente sem secrets
GITHUB_TOKEN = st.secrets.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
GITHUB_USER = st.secrets.get("GITHUB_USER") or os.environ.get("GITHUB_USER")
GITHUB_REPO = st.secrets.get("GITHUB_REPO") or os.environ.get("GITHUB_REPO")
ENCRYPTION_KEY = st.secrets.get("ENCRYPTION_KEY") or os.environ.get("ENCRYPTION_KEY")

GITHUB_FILE = "baseaud.csv"
# RAW_URL e API_URL são construídos mesmo que o arquivo ainda não exista no repo.
RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/{GITHUB_FILE}" if GITHUB_USER and GITHUB_REPO else None
API_URL = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{GITHUB_FILE}" if GITHUB_USER and GITHUB_REPO else None

# ---------------------------------------------------------
# VALIDAÇÕES INICIAIS (mensagens claras)
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
        "Faltam configurações necessárias: " + ", ".join(missing) + ".\n\n"
        "No ambiente de deploy (Streamlit Cloud) adicione essas chaves em Secrets. "
        "Para testes locais crie .streamlit/secrets.toml ou exporte variáveis de ambiente."
    )
    st.stop()

# inicializa Fernet
try:
    fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)
except Exception:
    st.error("Chave de criptografia inválida. Gere com Fernet.generate_key() e coloque em ENCRYPTION_KEY.")
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
@st.cache_data(ttl=1)
def load_csv_from_github():
    """
    Baixa o arquivo do GitHub via API (conteúdo em base64), tenta decodificar e
    descriptografar com Fernet. Se o arquivo não existir (404), retorna DataFrame vazio
    com colunas esperadas. Se o arquivo existir mas não for cifrado com a chave,
    tenta ler como texto plano (compatibilidade).
    """
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

    # 1) Tenta obter via API (retorna JSON com content em base64)
    r = requests.get(API_URL, headers=headers)
    if r.status_code == 404:
        # repositório sem base ainda — retorna DF vazio com colunas esperadas
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
# FUNÇÃO DE UPLOAD (CIFRA E ENVIA AO GITHUB) COM RETRY 409
# ---------------------------------------------------------
def upload_csv_to_github(uploaded_file):
    """
    Recebe UploadedFile do Streamlit, cifra os bytes com Fernet, codifica em base64
    e envia ao GitHub via API. Em caso de conflito (409), busca SHA novamente e reenvia.
    """
    if not GITHUB_TOKEN:
        st.error("GITHUB_TOKEN não configurado. Não é possível enviar ao GitHub.")
        return

    content = uploaded_file.getvalue()  # bytes do CSV
    # cifra os bytes
    encrypted = fernet.encrypt(content)
    # codifica em base64 para o campo 'content' da API
    encoded = base64.b64encode(encrypted).decode()

    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    def get_sha():
        r = requests.get(API_URL, headers=headers)
        if r.status_code == 200:
            return r.json().get("sha")
        return None

    sha = get_sha()

    def make_payload(sha_value):
        payload = {
            "message": "Atualização automática do CSV (cifrado) via Streamlit",
            "content": encoded,
            "branch": "main"
        }
        if sha_value:
            payload["sha"] = sha_value
        return payload

    payload = make_payload(sha)
    put_response = requests.put(API_URL, json=payload, headers=headers)

    # Se conflito, tenta buscar SHA de novo e reenviar
    if put_response.status_code == 409:
        new_sha = get_sha()
        payload = make_payload(new_sha)
        put_response = requests.put(API_URL, json=payload, headers=headers)

    if put_response.status_code in [200, 201]:
        st.success("CSV cifrado enviado com sucesso ao GitHub! Recarregando...")
        st.cache_data.clear()
        st.rerun()
    else:
        st.error(f"Erro ao enviar arquivo: {put_response.status_code} {put_response.text}")

# ---------------------------------------------------------
# MODO sisbase — UPLOAD DO CSV (CIFRADO)
# ---------------------------------------------------------
if password == "sisbase":
    st.header("🗂 Painel de Administração da Base")
    st.info("Envie um CSV; ele será cifrado localmente e armazenado cifrado no GitHub.")
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
    st.warning("Nenhuma base encontrada. Peça ao administrador para atualizar a base.")
    st.stop()

# converte coluna "data e horário" para datetime com dayfirst=True
df["data e horário"] = pd.to_datetime(df["data e horário"], dayfirst=True, errors="coerce")
df["dia"] = df["data e horário"].dt.strftime("%d/%m/%y")

df = df.sort_values(["dia", "sala de audiência", "data e horário"])

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
        st.markdown(f"### ⏰ {dt_str}")
        st.markdown(f"**Processo:** {row0.get('número do processo relacionado','')}")
        st.markdown(f"**Tipo:** {row0.get('parte a ser ouvida ou tipo de processo','')}")
        link = row0.get("link do processo", "")
        if link:
            st.markdown(f"[🔗 Link do processo]({link})")
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
            st.markdown(f"### 🏛 Sala {sala}")
            df_sala = df_dia[df_dia["sala de audiência"] == sala]
            for processo, bloco in df_sala.groupby("número do processo relacionado"):
                render_process_box(bloco, show_sensitive)

# ---------------------------------------------------------
# SECRETÁRIOS
# ---------------------------------------------------------
if password == "sissecret":
    st.header("📌 Painel dos Secretários")
    for dia in df["dia"].unique():
        df_dia = df[df["dia"] == dia]
        if any(df_dia["sala de audiência"].isin(salas_selecionadas)):
            st.markdown(f"## 📅 {dia}")
            render_day(df_dia, show_sensitive=True)

# ---------------------------------------------------------
# AUTORIDADES
# ---------------------------------------------------------
elif password == "sisautoridades":
    st.header("⚖ Painel das Autoridades")
    for dia in df["dia"].unique():
        df_dia = df[df["dia"] == dia]
        if any(df_dia["sala de audiência"].isin(salas_selecionadas)):
            st.markdown(f"## 📅 {dia}")
            render_day(df_dia, show_sensitive=False)

# ---------------------------------------------------------
# ACESSO NEGADO
# ---------------------------------------------------------
elif password.strip() != "":
    st.error("Chave inválida.")