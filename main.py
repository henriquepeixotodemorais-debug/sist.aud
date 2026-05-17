import os
import io
import base64
import requests
import streamlit as st
import pandas as pd
from cryptography.fernet import Fernet, InvalidToken
import re
import openpyxl

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
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

    r = requests.get(API_URL, headers=headers)
    if r.status_code == 404:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)
    if r.status_code != 200:
        st.error(f"Erro ao acessar GitHub API: {r.status_code} {r.text}")
        st.stop()

    payload = r.json()
    content_b64 = payload.get("content", "")
    if not content_b64:
        st.error("Conteúdo vazio no GitHub.")
        st.stop()

    content_b64 = "".join(content_b64.splitlines())
    try:
        raw_bytes = base64.b64decode(content_b64)
    except Exception as e:
        st.error(f"Erro ao decodificar base64: {e}")
        st.stop()

    # tenta descriptografar
    try:
        plain_bytes = fernet.decrypt(raw_bytes)
        text = plain_bytes.decode("utf-8", errors="replace")
    except InvalidToken:
        # fallback: arquivo não cifrado
        text = raw_bytes.decode("utf-8", errors="replace")

    # remove BOM se existir
    if text.startswith("\ufeff"):
        text = text.replace("\ufeff", "", 1)

    # normaliza quebras de linha
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # tenta ler com vírgula
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, sep=",")
        if df.shape[1] == 1:
            raise ValueError("provavelmente não é vírgula")
    except Exception:
        # tenta ler com ponto e vírgula
        df = pd.read_csv(io.StringIO(text), dtype=str, sep=";")

    # garante colunas esperadas
    for col in EXPECTED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

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
# df["dia"] = df["data e horário"].dt.nomalize()
df["dia"] = df["data e horário"].dt.date
# df["dia"] = df["data e horário"].dt.strftime("%d/%m/%y")

df = df.sort_values(["sala de audiência", "data e horário"])


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
# FILTRO DE DIA
# ---------------------------------------------------------
todos_dias = sorted(df["dia"].unique())
# df["dia"] = df["data e horário"].dt.strftime("%d/%m/%y")
dias_selecionados = st.multiselect(
    "Filtrar dia:",
    options=todos_dias,
    default=todos_dias,
    format_func=lambda d: d.strftime("%d/%m/%y")

)

if len(dias_selecionados) == 0:
    st.warning("Selecione ao menos um dia.")
    st.stop()

# ---------------------------------------------------------
# FUNÇÃO PARA MONTAR O BOX DE CADA PROCESSO
# ---------------------------------------------------------
def render_process_box(process_df, show_sensitive=False):
    row0 = process_df.iloc[0]

    with st.container():
        dt = row0["data e horário"]
        dt_str = dt.strftime("%H:%M") if pd.notna(dt) else row0.get("data e horário", "")
        # dt_str = dt.strftime("%d/%m/%Y %H:%M") if pd.notna(dt) else row0.get("data e horário", "")
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
            # st.markdown(f"##{len(df_sala["número do processo relacionado"])}")
            st.metric(label="",value="",delta=f"processos: {len(df_sala["número do processo relacionado"])}",delta_color="off")
            # st.markdown(f"{df_sala.groupby('data e horário')['processos'].nunique()}")

            for processo, bloco in df_sala.groupby("data e horário"):
            # for processo, bloco in df_sala.groupby("número do processo relacionado"):
                render_process_box(bloco, show_sensitive)

# ---------------------------------------------------------
# SECRETÁRIOS
# ---------------------------------------------------------
if password == SENHA_SECRETARIOS:
    st.set_page_config(page_title="Painel de Salas e Processos", layout="wide")

    RE_DATE_TIME = re.compile(r"(\d{2}/\d{2}/\d{2})\s+(\d{2}:\d{2})")
    RE_URL = re.compile(r"(https?://\S+)", re.IGNORECASE)

    # =========================
    # FIELD (copiar nativo reduzido)
    # =========================
    def show_field(label, value):
        v = "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)
        st.markdown(
            f"<div style='font-size:12px; font-weight:600; color:#666; margin-top:2px;'>{label}</div>",
            unsafe_allow_html=True
        )
        st.code(v)

    # =========================
    # WHATSAPP (telefones únicos + 1 link por número)
    # =========================
    def digits_only(s):
        return re.sub(r"\D+", "", str(s))


    def whatsapp_links(contatos):
        if contatos is None or (isinstance(contatos, float) and pd.isna(contatos)):
            return []

        raw = str(contatos)

        encontrados = re.findall(r"(?:\+?55)?\D?\(?\d{2}\)?\s?\d{4,5}-?\d{4}", raw)

        resultado = []
        vistos = set()

        for n in encontrados:
            numero = digits_only(n)
            if not numero:
                continue

            if len(numero) in (10, 11) and not numero.startswith("55"):
                numero = "55" + numero

            url = f"https://wa.me/{numero}"

            if url in vistos:
                continue

            vistos.add(url)
            resultado.append((n.strip(), url))

        return resultado

    # =========================
    # PARSING
    # =========================
    def is_header(nome):
        if pd.isna(nome):
            return False
        return not str(nome).strip().startswith("(")


    def extract_info(tl):
        if pd.isna(tl):
            return None, None, None

        s = str(tl)

        m = RE_DATE_TIME.search(s)
        data = m.group(1) if m else None
        hora = m.group(2) if m else None

        u = RE_URL.search(s)
        link = u.group(1) if u else None

        return data, hora, link


    def build_index(df):
        rows = []

        # sort=False para respeitar a ordem original do arquivo
        for proc, g in df.groupby("Processos", sort=False):
            g = g.reset_index(drop=True)

            header_idx = None
            for i in range(len(g)):
                if is_header(g.loc[i, "Nome"]):
                    header_idx = i
                    break

            if header_idx is None:
                continue

            header = g.loc[header_idx]
            data, hora, link = extract_info(header.get("tl"))

            sala_raw = header.get("sala")
            sala = str(int(sala_raw)) if sala_raw is not None and not (isinstance(sala_raw, float) and pd.isna(sala_raw)) else None

            partes = g.drop(index=header_idx)

            rows.append({
                "Processos": str(proc),
                "sala": sala,
                "data": data,
                "hora": hora,
                "link": link,
                "partes": partes
            })

        return pd.DataFrame(rows)


    def filter_index(idx, salas, datas, processos):
        q = idx.copy()

        if salas:
            q = q[q["sala"].isin(salas)]

        if datas:
            q = q[q["data"].isin(datas)]

        if processos:
            q = q[q["Processos"].isin(processos)]

        return q

    # =========================
    # CACHE
    # =========================
    @st.cache_data(persist="disk")
    def load_df(file_bytes):
        return pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")


    @st.cache_data(persist="disk")
    def load_idx(df):
        return build_index(df)

    # =========================
    # APP
    # =========================
    file = st.file_uploader("Envie o Excel")

    st.title("📌 Painel dos Secretários")

    if not file:
        st.stop()

    df = load_df(file.getvalue())
    idx = load_idx(df)

    # =========================
    # FILTROS (sidebar com separação clara)
    # =========================
    st.sidebar.markdown("## 🎛️ Filtros")
    st.sidebar.divider()

    salas_all = sorted(idx["sala"].dropna().unique().tolist())
    datas_all = sorted(idx["data"].dropna().unique().tolist())

    st.sidebar.markdown("### 🏛️ Salas")
    salas_sel = st.sidebar.multiselect("Selecionar salas", salas_all, salas_all)

    st.sidebar.divider()

    st.sidebar.markdown("### 📅 Datas")
    datas_sel = []
    for d in datas_all:
        if st.sidebar.checkbox(d, True):
            datas_sel.append(d)

    st.sidebar.divider()

    # ✅ Processos só depois de aplicar Sala + Data
    idx_sd = idx.copy()
    if salas_sel:
        idx_sd = idx_sd[idx_sd["sala"].isin(salas_sel)]
    if datas_sel:
        idx_sd = idx_sd[idx_sd["data"].isin(datas_sel)]

    processos_disponiveis = sorted(idx_sd["Processos"].dropna().unique().tolist())

    st.sidebar.markdown("### 📄 Processos (após Sala + Data)")
    processos_sel = st.sidebar.multiselect(
        "Selecionar processos",
        processos_disponiveis,
        processos_disponiveis
    )

    view = filter_index(idx, salas_sel, datas_sel, processos_sel)

    if view.empty:
        st.warning("Nada para exibir.")
        st.stop()

    # =========================
    # RENDER
    # =========================
    cols = st.columns(len(salas_sel)) if salas_sel else [st.container()]

    for col, sala in zip(cols, salas_sel):
        with col:
            # ✅ Sala com mais destaque
            st.markdown(
                f"<div style='font-size:30px; font-weight:900; margin:2px 0 6px 0;'>Sala {sala}</div>",
                unsafe_allow_html=True
            )

            base = view[view["sala"] == sala].copy()

            for data in sorted(base["data"].dropna().unique()):
                # ✅ Data com destaque
                st.markdown(
                    f"<div style='font-size:22px; font-weight:800; margin:10px 0 6px 0;'>📅 {data}</div>",
                    unsafe_allow_html=True
                )

                dia = base[base["data"] == data].copy()

                # ✅ Ordena por horário (e mantém o resto estável)
                dia["ordem"] = dia["hora"].fillna("99:99")
                dia = dia.sort_values("ordem").drop(columns=["ordem"])

                for _, row in dia.iterrows():
                    # ✅ Box do processo
                    with st.container(border=True):
                        hora = row.get("hora") or ""
                        proc = row.get("Processos") or ""
                        link = row.get("link") or ""

                        # ✅ Hora com destaque
                        st.markdown(
                            f"<div style='font-size:20px; font-weight:900; margin:2px 0 8px 0;'>Hora: {hora}</div>",
                            unsafe_allow_html=True
                        )

                        # ✅ Processo com destaque
                        st.markdown(
                            f"<div style='font-size:18px; font-weight:900; margin:2px 0 10px 0;'>Processo: {proc}</div>",
                            unsafe_allow_html=True
                        )

                        # ✅ Abrir sala bem destacado
                        if link:
                            st.markdown(
                                f"<div style='font-size:16px; font-weight:800; margin:0 0 8px 0;'>"
                                f"<a href='{link}' target='_blank'>🔗 Abrir sala</a>"
                                f"</div>",
                                unsafe_allow_html=True
                            )

                        # ✅ “Copiar todos os nomes” (estável): campo com todos os nomes
                        partes_df = row.get("partes")
                        if partes_df is not None and len(partes_df) > 0:
                            nomes = []
                            for n in partes_df.get("Nome", pd.Series(dtype=str)).tolist():
                                if n is None or (isinstance(n, float) and pd.isna(n)):
                                    continue
                                s = str(n).strip()
                                if s:
                                    nomes.append(s)

                            if nomes:
                                st.markdown(
                                    "<div style='font-size:12px; font-weight:700; color:#444; margin-top:6px;'>"
                                    "Copiar todos os nomes.</div>",
                                    unsafe_allow_html=True
                                )
                                st.code("\n".join(nomes))

                        # Partes (menor destaque)
                        if partes_df is None or len(partes_df) == 0:
                            st.caption("—")
                            continue

                        for _, part in partes_df.reset_index(drop=True).iterrows():
                            st.divider()

                            show_field("Nome", part.get("Nome"))
                            show_field("Dados da parte", part.get("Dados da parte"))
                            show_field("Endereço", part.get("Endereço"))

                            st.markdown(
                                "<div style='font-size:12px; font-weight:600; color:#666; margin-top:2px;'>Contatos</div>",
                                unsafe_allow_html=True
                            )

                            links = whatsapp_links(part.get("Contatos"))

                            if links:
                                for txt, url in links:
                                    st.markdown(f"- {txt} → {url}")
                            else:
                                st.caption("—")

                            show_field("Copiar contatos", part.get("Contatos"))

                            if not pd.isna(part.get("lc")):
                                show_field("Local", part.get("lc"))

                            if not pd.isna(part.get("et")):
                                show_field("Entrevistador", part.get("et"))

                            if not pd.isna(part.get("tl da intimação")):
                                show_field("Telefone da intimação", part.get("tl da intimação"))


# ---------------------------------------------------------
# AUTORIDADES
# ---------------------------------------------------------
elif password == SENHA_AUTORIDADES:
    st.header("⚖ Painel das Autoridades - Audiências")
    das = df[df["dia"].isin(dias_selecionados)]

    for dia in sorted(das["dia"].unique()):
        df_dia = das[das["dia"] == dia].sort_values(by="data e horário")

        if any(df_dia["sala de audiência"].isin(salas_selecionadas)):
            st.divider()
            # st.markdown(f"# 📅 {dia}")
            st.markdown(f"# 📅 {str(dia).split("-")[2]}/{str(dia).split("-")[1]}/{str(dia).split("-")[0]}")
            render_day(df_dia, show_sensitive=False)


# ---------------------------------------------------------
# ACESSO NEGADO
# ---------------------------------------------------------
elif password.strip() != "":
    st.error("Chave inválida.")