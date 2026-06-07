import streamlit as st
import pandas as pd
import json
import os
import re
import tempfile
import unicodedata
import pdfplumber
import io
from datetime import date
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from supabase import create_client
from google import genai
from google.genai import types

# --- 1. CONFIGURAÇÕES ---
SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
GEMINI_KEY   = st.secrets["GEMINI_KEY"]

# --- 2. INICIALIZAÇÃO ---
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
client   = genai.Client(api_key=GEMINI_KEY)

# Restaura sessão do usuário entre re-runs do Streamlit
if "sb_access_token" in st.session_state:
    try:
        supabase.auth.set_session(
            st.session_state["sb_access_token"],
            st.session_state["sb_refresh_token"],
        )
        # Propaga o token explicitamente para o cliente PostgREST
        supabase.postgrest.auth(st.session_state["sb_access_token"])
    except Exception:
        for _k in ["sb_access_token", "sb_refresh_token", "familia_id"]:
            st.session_state.pop(_k, None)

# --- Google OAuth ---
_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]
_GOOGLE_REDIRECT = "http://localhost:8501/"
_GOOGLE_CREDS_FILE = "google_credentials.json"
_GOOGLE_TOKEN_FILE = "google_token.json"
_OAUTH_STATE_DIR = Path(tempfile.gettempdir()) / "cf_oauth_states"

# Handler do callback OAuth — deve rodar ANTES do auth check para restaurar sessão
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
_qp = st.query_params
if "code" in _qp and "state" in _qp:
    _state_file = _OAUTH_STATE_DIR / f"{_qp['state']}.json"
    if _state_file.exists():
        try:
            _state_data = json.loads(_state_file.read_text())
            _state_file.unlink()
            for _k in ("sb_access_token", "sb_refresh_token", "familia_id"):
                if _state_data.get(_k):
                    st.session_state[_k] = _state_data[_k]
            if _state_data.get("sb_access_token"):
                supabase.auth.set_session(
                    _state_data["sb_access_token"],
                    _state_data["sb_refresh_token"],
                )
                supabase.postgrest.auth(_state_data["sb_access_token"])
            from google_auth_oauthlib.flow import Flow as _GFlow
            _flow = _GFlow.from_client_secrets_file(
                _GOOGLE_CREDS_FILE,
                scopes=_GOOGLE_SCOPES,
                redirect_uri=_GOOGLE_REDIRECT,
            )
            _flow.fetch_token(code=_qp["code"])
            _creds = _flow.credentials
            _token_data = {
                "token":         _creds.token,
                "refresh_token": _creds.refresh_token,
                "token_uri":     _creds.token_uri,
                "client_id":     _creds.client_id,
                "client_secret": _creds.client_secret,
                "scopes":        list(_creds.scopes or []),
                "expiry":        _creds.expiry.isoformat() if _creds.expiry else None,
            }
            with open(_GOOGLE_TOKEN_FILE, "w") as _f:
                json.dump(_token_data, _f)
            st.session_state["google_token"] = _token_data
            st.session_state["google_conectado"] = True
            st.session_state["modulo_ativo"] = _state_data.get("modulo_origem", "📅 Agenda")
        except Exception as _e:
            st.session_state["google_oauth_error"] = str(_e)
            st.session_state["modulo_ativo"] = _state_data.get("modulo_origem", "📅 Agenda")
        st.query_params.clear()
        st.rerun()

TIPOS_PAGAMENTO = {
    "01": "01 - Cartão de Crédito",
    "02": "02 - Pix",
    "03": "03 - Débito Automático",
    "04": "04 - TED e Dinheiro",
    "05": "05 - Débito em Conta",
    "06": "06 - Boleto Bancário",
}
TIPOS_PAGAMENTO_LISTA = list(TIPOS_PAGAMENTO.values())
TIPOS_NAO_CARTAO      = [v for k, v in TIPOS_PAGAMENTO.items() if k != "01"]
COD_CARTAO            = "01"

TIPOS_CATEGORIA = [
    "",
    "1 - Despesas fixas essenciais",
    "2 - Despesas fixas comuns",
    "3 - Despesas eventuais",
    "4 - Despesas extras",
    "5 - Despesas fixas temporárias",
]

PERIODICIDADES = ["", "Fixa Mensal", "Eventual"]
IMPORTANCIAS   = ["", "Essencial", "Não essencial"]

# --- 3. FUNÇÕES ---

def limpar_texto(texto: str) -> str:
    linhas = texto.splitlines()
    linhas = [l.strip() for l in linhas if l.strip()]
    linhas = [l for l in linhas if not re.fullmatch(r"[-=_*]{3,}|\d{1,3}", l)]
    return "\n".join(linhas)


def detectar_tipo_arquivo(file_name: str) -> str:
    nome = file_name.upper()
    if "EXTRATO" in nome:
        return "extrato"
    if "CARTÃO" in nome or "CARTAO" in nome or "CATÃO" in nome or "CATAO" in nome:
        return "fatura"
    return "fatura"


def cartao_do_arquivo(file_name: str, config: dict) -> str:
    nome = file_name.upper()
    for inst in config["instituicoes"]:
        if inst["tipo"] == "fatura":
            if any(p.upper() in nome for p in inst["padroes_arquivo"]):
                return inst["nome"]
    return "Desconhecido"


def banco_do_arquivo(file_name: str, config: dict) -> str:
    nome = file_name.upper()
    for inst in config["instituicoes"]:
        if inst["tipo"] == "extrato":
            if any(p.upper() in nome for p in inst["padroes_arquivo"]):
                return inst["nome"]
    return "Banco"


def buscar_regras_db() -> list:
    res = supabase.table("regras_classificacao").select("*").eq("familia_id", FAMILIA_ID).order("categoria").execute()
    return res.data


def buscar_categorias_db() -> list:
    res = supabase.table("categorias").select("*").eq("familia_id", FAMILIA_ID).order("categoria").execute()
    return res.data


def buscar_lancamentos_db() -> list:
    res = supabase.table("lancamentos").select("*").eq("familia_id", FAMILIA_ID).order("data_origem", desc=True).execute()
    return res.data


@st.cache_data(ttl=300)
def buscar_config_familia(familia_id: str) -> dict:
    """Carrega toda a configuração da família via RPC SECURITY DEFINER (não depende de RLS)."""
    res  = supabase.rpc("get_config_familia", {"p_familia_id": familia_id}).execute()
    data = res.data or {}
    return {
        "instituicoes":      data.get("instituicoes")      or [],
        "mapeamentos":       data.get("mapeamentos")       or [],
        "ignorados":         data.get("ignorados")         or [],
        "fixos":             data.get("fixos")             or [],
        "membros":           data.get("membros")           or [],
        "categorias_padrao": data.get("categorias_padrao") or [],
    }


def aplicar_categorias(df: pd.DataFrame, regras: list, categorias_padrao: list | None = None) -> pd.DataFrame:
    df = df.copy()
    # Inicializa colunas apenas se não existirem — preserva valores já definidos
    # (ex: lançamentos fixos mensais que chegam com categoria preenchida)
    if "categoria" not in df.columns:
        df["categoria"] = "Outros"
    else:
        df["categoria"] = df["categoria"].fillna("").replace("", "Outros")
    if "subcategoria" not in df.columns:
        df["subcategoria"] = "Outros"
    else:
        df["subcategoria"] = df["subcategoria"].fillna("").replace("", "Outros")
    if "descricao" not in df.columns:
        df["descricao"] = ""
    if "tipo_categoria" not in df.columns:
        df["tipo_categoria"] = ""
    if "periodicidade" not in df.columns:
        df["periodicidade"] = ""
    if "importancia" not in df.columns:
        df["importancia"] = ""
    if "comprado_para" not in df.columns:
        df["comprado_para"] = ""

    for idx, row in df.iterrows():
        # Lançamentos que já têm categoria definida (ex: fixos mensais) não são sobrescritos
        cat_atual = str(row.get("categoria", "")).strip()
        if cat_atual and cat_atual != "Outros":
            continue

        est = str(row["estabelecimento"]).upper()
        for r in regras:
            if r["palavra_chave"].upper() in est:
                df.at[idx, "categoria"]    = r["categoria"]
                df.at[idx, "subcategoria"] = r.get("subcategoria", "Outros")
                # Propaga descrição se preenchida na regra
                desc_regra = (r.get("descricao") or "").strip()
                if desc_regra:
                    df.at[idx, "descricao"] = desc_regra
                # Propaga estabelecimento se preenchido na regra
                estab_regra = (r.get("estabelecimento") or "").strip()
                if estab_regra:
                    df.at[idx, "estabelecimento"] = estab_regra
                # Propaga tipo_pagamento se preenchido na regra
                tipo_regra = (r.get("tipo_pagamento") or "").strip()
                if tipo_regra:
                    df.at[idx, "tipo_pagamento"] = tipo_regra
                # Propaga tipo_categoria se preenchido na regra
                tipo_cat_regra = (r.get("tipo_categoria") or "").strip()
                if tipo_cat_regra:
                    df.at[idx, "tipo_categoria"] = tipo_cat_regra
                # Propaga periodicidade se preenchida na regra
                per_regra = (r.get("periodicidade") or "").strip()
                if per_regra:
                    df.at[idx, "periodicidade"] = per_regra
                # Propaga importancia se preenchida na regra
                imp_regra = (r.get("importancia") or "").strip()
                if imp_regra:
                    df.at[idx, "importancia"] = imp_regra
                # Propaga comprado_para se preenchido na regra
                comp_para_regra = (r.get("comprado_para") or "").strip()
                if comp_para_regra:
                    df.at[idx, "comprado_para"] = comp_para_regra
                break

        # Fallback: se ainda sem categoria, usa a categoria padrão do comprado_para
        if categorias_padrao and str(df.at[idx, "categoria"]).strip() in ("", "Outros"):
            comp_para = str(df.at[idx, "comprado_para"]).strip()
            if comp_para:
                padrao = next((cp for cp in categorias_padrao if cp["comprado_para"] == comp_para), None)
                if padrao:
                    df.at[idx, "categoria"]   = padrao["categoria"]
                    df.at[idx, "subcategoria"] = padrao["subcategoria"]
    return df


def _corrigir_data(d: dict, ano_ref: str, mes_ref_num: str):
    try:
        date.fromisoformat(d["data_origem"])
    except (ValueError, KeyError):
        import calendar
        ultimo = calendar.monthrange(int(ano_ref), int(mes_ref_num))[1]
        d["data_origem"] = f"{ano_ref}-{mes_ref_num}-{ultimo:02d}"


def normalizar_comprador(nome: str, config: dict) -> str:
    """Normaliza o nome do comprador usando os membros cadastrados no banco."""
    if not nome:
        return ""
    nomes_validos = {m["nome"] for m in config["membros"]}
    if nome in nomes_validos:
        return nome
    nome_up = nome.upper().replace(" ", "")
    nome_sem_acento = _sem_acento(nome_up)
    for membro in config["membros"]:
        m_up = membro["nome"].upper().replace(" ", "")
        if _sem_acento(m_up) in nome_sem_acento or nome_sem_acento in _sem_acento(m_up):
            return membro["nome"]
    return ""


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def resolver_comprador(nome_fatura: str, cartao: str, config: dict) -> str:
    """Resolve o nome do comprador usando o mapeamento do banco para o cartão."""
    nome_up = nome_fatura.upper()
    for m in config["mapeamentos"]:
        if m["cartao"] == cartao and m["nome_na_fatura"].upper() in nome_up:
            return m["nome_membro"]
    return normalizar_comprador(nome_fatura, config)


def _chamar_ia_fatura(prompt: str) -> list:
    """Chama a IA e retorna lista de lançamentos. Lança exceção se falhar."""
    response = client.models.generate_content(
        model="gemini-2.5-flash", contents=prompt,
        config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0)),
    )
    return json.loads(re.sub(r"```[\w]*", "", response.text).strip())


def processar_fatura(file_name: str, file_bytes: bytes, mes_referencia: str, config: dict) -> list:
    cartao     = cartao_do_arquivo(file_name, config)
    if cartao == "Desconhecido":
        nomes_cartoes = [i["nome"] for i in config["instituicoes"] if i["tipo"] == "fatura"]
        raise ValueError(
            f"Cartão não reconhecido para o arquivo '{file_name}'. "
            f"Cartões configurados: {', '.join(nomes_cartoes)}."
        )
    inst_info = next((i for i in config["instituicoes"] if i["nome"] == cartao), {})
    comprador_fixo           = inst_info.get("comprador_fixo")
    tem_multiplos_portadores = inst_info.get("tem_multiplos_portadores", False)

    # Extrai texto por página para permitir processamento em lotes
    paginas = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for p in pdf.pages:
            t = limpar_texto(p.extract_text() or "")
            if t.strip():
                paginas.append(t)

    # Agrupa páginas em blocos de até 2 páginas para não exceder contexto
    BLOCO = 2
    blocos = [paginas[i:i+BLOCO] for i in range(0, len(paginas), BLOCO)]

    instrucoes_base = (
        "Analise o trecho de fatura de cartão de crédito abaixo.\n"
        "Extraia TODOS os lançamentos presentes neste trecho, "
        "independentemente da data de compra.\n"
        "INCLUA: compras à vista, parcelas que vencem nesta fatura, "
        "tarifas, anuidades, IOF, lançamentos internacionais e produtos/serviços.\n"
        "NÃO INCLUA: lançamentos da seção \'Compras parceladas - próximas faturas\'.\n"
        "ATENÇÃO CRÍTICA: A fatura pode conter ao final uma seção intitulada "
        "\'Compras parceladas - próximas faturas\', \'Próxima fatura\' ou \'Demais faturas\'. "
        "Essa seção lista parcelas FUTURAS que NÃO devem ser incluídas. "
        "Ao encontrar qualquer um desses títulos, IGNORE TODO o conteúdo que se segue até o fim do trecho.\n"
        "Se um estabelecimento aparecer com duas versões de parcela (ex: \'01/03\' e \'02/03\'), "
        "inclua APENAS a parcela com o número menor (a atual). Nunca inclua a parcela seguinte.\n"
        "NÃO INCLUA: pagamentos efetuados (créditos de pagamento de fatura).\n"
        "Se este trecho não contiver lançamentos, retorne um array vazio [].\n"
        "Use o valor exato de cada lançamento. Inclua cancelamentos (valores negativos).\n"
        "Para lançamentos internacionais use o valor em R$ (BRL), não em USD.\n"
        "O campo data_origem deve ser a data da compra original conforme aparece na fatura.\n"
        "Se o ano não aparecer na data, use o ano da fatura ou o ano anterior quando "
        "a data for maior que o mês de vencimento.\n"
        "O campo estabelecimento deve ser COPIADO LITERALMENTE da fatura, SEM abreviar.\n"
    )

    if tem_multiplos_portadores:
        estrutura = '[{"data_origem":"AAAA-MM-DD","estabelecimento":"NOME","valor_parcela":0.0,"descricao":"","comprado_por":"NOME DO PORTADOR"}]'
        extra = (
            "A fatura pode ter seções por portador ou por número de cartão.\n"
            "Identifique o nome do portador pelo cabeçalho de cada seção — "
            "pode aparecer como 'NOME DO PORTADOR', 'Cartão XXXX - NOME' ou similar.\n"
            "Quando múltiplos cartões pertencem ao mesmo titular (ex: 'Cartão Final 0513 - FABRICIO', "
            "'Cartão Final 4315 - FABRICIO'), use o nome desse titular para todos.\n"
            "Para seção de 'cartões adicionais', use o nome do portador do cartão adicional.\n"
            "Inclua estornos como valores NEGATIVOS (ex: 'Estorno Tarifa' → valor_parcela negativo).\n"
            "NÃO inclua linhas de IOF listadas separadamente — o IOF já está embutido no valor da compra.\n"
        )
    else:
        estrutura = '[{"data_origem":"AAAA-MM-DD","estabelecimento":"NOME","valor_parcela":0.0,"descricao":""}]'
        extra = ""

    sufixo = (
        extra
        + "Retorne EXCLUSIVAMENTE um array JSON válido, sem texto adicional nem blocos de código.\n"
        + f"Estrutura: {estrutura}\n\n"
    )

    # Processa cada bloco e consolida
    todos_dados = []
    erros_blocos = []
    for bloco in blocos:
        texto_bloco = "\n\n".join(bloco)
        prompt = instrucoes_base + sufixo + f"Trecho da fatura:\n{texto_bloco}"
        try:
            parcial = _chamar_ia_fatura(prompt)
            todos_dados.extend(parcial)
        except Exception as _e_bloco:
            erros_blocos.append(str(_e_bloco))

    if erros_blocos and not todos_dados:
        raise RuntimeError(
            f"Falha ao processar '{file_name}' via IA. "
            f"Erros: {'; '.join(erros_blocos[:2])}"
        )

    # Fallback: se texto não rendeu nada, envia o PDF inteiro ao Gemini como arquivo
    if not todos_dados:
        instrucao_direta = (
            instrucoes_base
            + "ATENÇÃO: ignore completamente seções de 'Opções de pagamento', "
            "'Pagamento de fatura', 'Parcelamento', 'FAQ' e similares. "
            "Extraia SOMENTE as transações de compra listadas nas seções "
            "'Transações do cartão', 'Lançamentos' ou similares.\n"
            "NÃO inclua: pagamentos de fatura (ex: 'Pag Fatura Boleto'), "
            "IOF e taxas listados como linhas separadas de transações.\n"
            + sufixo
        )
        try:
            _arq = client.files.upload(
                file=io.BytesIO(file_bytes),
                config=types.UploadFileConfig(mime_type="application/pdf", display_name=file_name),
            )
            _resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_uri(file_uri=_arq.uri, mime_type="application/pdf"),
                    instrucao_direta,
                ],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0)
                ),
            )
            todos_dados = json.loads(re.sub(r"```[\w]*|```", "", _resp.text).strip())
        except Exception as _e_fb:
            raise RuntimeError(
                f"Falha ao processar '{file_name}' (texto e upload): {_e_fb}"
            )

    ignorados_fatura = [e["texto"].upper() for e in config["ignorados"] if e["tipo"] == "fatura"]

    # Remove somente triplicatas+ (mesma data+estabelecimento+valor aparecendo 3+ vezes indica erro da IA)
    # Duplicatas legítimas são permitidas (ex: mesma loja comprada duas vezes no mesmo dia)
    contagem: dict = {}
    dados = []
    for d in todos_dados:
        estab_up = (d.get("estabelecimento") or "").upper()
        if any(ign in estab_up for ign in ignorados_fatura):
            continue
        chave = (d.get("data_origem",""), d.get("estabelecimento",""), d.get("valor_parcela",0))
        contagem[chave] = contagem.get(chave, 0) + 1
        if contagem[chave] <= 2:
            dados.append(d)

    ano, mes, _ = mes_referencia.split("-")
    for d in dados:
        d["mes_referencia"] = mes_referencia
        d["cartao"]         = cartao
        d["tipo_pagamento"] = COD_CARTAO
        d.setdefault("descricao", "")
        _corrigir_data(d, ano, mes)
        _sanitizar_registro(d)

        if comprador_fixo:
            d["comprado_por"] = comprador_fixo
        elif tem_multiplos_portadores:
            nome_ia = d.get("comprado_por", "")
            d["comprado_por"] = resolver_comprador(nome_ia, cartao, config)
        else:
            d["comprado_por"] = ""

        d["comprado_por"] = normalizar_comprador(d.get("comprado_por", ""), config)
        # Para faturas de cartão, comprado_para recebe o mesmo valor de comprado_por
        d["comprado_para"] = d["comprado_por"]

    return dados


def _sanitizar_registro(d: dict) -> dict:
    """Converte NaN, Infinity e -Infinity para 0.0 — garante JSON válido."""
    import math
    for k, v in d.items():
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            d[k] = 0.0
    return d


def _extrair_linhas_extrato_itau(file_bytes: bytes, config: dict) -> list:
    """
    Extrai lançamentos do extrato Itaú usando posição Y das palavras no PDF.
    Garante que a descrição seja copiada exatamente como aparece no extrato.
    """
    import re as _re
    resultado = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for pagina in pdf.pages:
            words = pagina.extract_words(
                keep_blank_chars=False, x_tolerance=3, y_tolerance=3
            )
            # Agrupa palavras por linha (mesmo Y arredondado)
            linhas = {}
            for w in words:
                y = round(w["top"] / 5) * 5
                linhas.setdefault(y, []).append(w)

            for y in sorted(linhas.keys()):
                texto_linha = " ".join(
                    w["text"] for w in sorted(linhas[y], key=lambda w: w["x0"])
                )
                # Filtra somente linhas que começam com data DD/MM/AAAA
                m = _re.match(
                    r"(\d{2})/(\d{2})/(\d{4})\s+(.+?)\s+(-[\d\.]+,[\d]{2})(?:\s|$)",
                    texto_linha.strip(),
                )
                if not m:
                    continue
                dia, mes_ext, ano_ext, desc, val_str = m.groups()
                # Ignora entradas (valores positivos não têm sinal -)
                # val_str sempre começa com - (débito)
                try:
                    valor = abs(float(val_str.replace(".", "").replace(",", ".")))
                except ValueError:
                    continue
                # Ignora pagamentos de fatura de cartão e lançamentos substituídos por regras internas
                desc_up = desc.upper()
                ignorados_extrato = [e["texto"].upper() for e in config["ignorados"] if e["tipo"] == "extrato"]
                ignorados_fatura  = [e["texto"].upper() for e in config["ignorados"] if e["tipo"] == "fatura"]
                ignorar = any(p in desc_up for p in [
                    "FATURA", "ITAU BLACK", "ITAUBLACK",
                ] + ignorados_extrato) \
                    or any(p in desc_up for p in ignorados_fatura)
                if ignorar:
                    continue
                resultado.append({
                    "data_origem":     f"{ano_ext}-{mes_ext}-{dia}",
                    "estabelecimento": desc.strip(),
                    "valor_parcela":   valor,
                })

    # PDFs Itaú com dupla camada (texto + imagem) geram o mesmo lançamento em
    # posições Y ligeiramente diferentes → dois grupos distintos → entrada duplicada.
    seen_keys: set = set()
    unique: list = []
    for item in resultado:
        key = (item["data_origem"], item["estabelecimento"], item["valor_parcela"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(item)
    return unique


def processar_extrato(file_name: str, file_bytes: bytes, mes_referencia: str, config: dict) -> list:
    banco = banco_do_arquivo(file_name, config)
    ano, mes, _ = mes_referencia.split("-")

    inst_info         = next((i for i in config["instituicoes"] if i["nome"] == banco), {})
    comprador_padrao  = inst_info.get("comprador_padrao") or ""
    ignorados_extrato = [e["texto"] for e in config["ignorados"] if e["tipo"] == "extrato"]
    ignorados_fatura  = [e["texto"] for e in config["ignorados"] if e["tipo"] == "fatura"]

    # Extração direta — preserva descrição exata do extrato
    if any(p.upper() in file_name.upper() for p in inst_info.get("padroes_arquivo", []) if "ITAU" in p.upper() or "ITAÚ" in p.upper()):
        dados = _extrair_linhas_extrato_itau(file_bytes, config)
    else:
        dados = []

    if not dados:
        # Fallback IA para CEF, outros bancos ou se extração direta falhar
        texto = limpar_texto("".join(
            (p.extract_text() or "") + "\n"
            for p in pdfplumber.open(io.BytesIO(file_bytes)).pages
        ))
        ignorar_extrato_str = "\n".join(f'- "{p}"' for p in ignorados_extrato)
        instrucao = (
            f"Analise este extrato bancário do {banco}.\n"
            "Extraia SOMENTE os lançamentos de débito (saídas/pagamentos). NÃO inclua depósitos, entradas ou saldo.\n"
            "NÃO inclua pagamentos de fatura de cartão de crédito.\n"
            f"NÃO inclua lançamentos cuja descrição contenha qualquer um dos textos abaixo:\n{ignorar_extrato_str}\n"
            "REGRA CRÍTICA: O campo estabelecimento deve ser COPIADO LITERALMENTE da coluna de "
            "descrição do extrato, SEM abreviar, SEM traduzir, SEM interpretar.\n"
            "O campo descricao deve ficar vazio.\n"
            "Retorne EXCLUSIVAMENTE um array JSON válido, sem texto adicional nem blocos de código.\n"
            "Se não houver lançamentos de débito, retorne um array vazio: []\n"
            'Estrutura: [{"data_origem":"AAAA-MM-DD","estabelecimento":"COPIA LITERAL DO EXTRATO","valor_parcela":0.0,"descricao":""}]\n\n'
        )

        if texto.strip():
            # PDF com texto extraível — envia o texto para a IA
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=instrucao + f"Extrato:\n{texto}",
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0)
                ),
            )
        else:
            # PDF escaneado (imagem) — envia o arquivo diretamente para a IA via upload
            arquivo_gemini = client.files.upload(
                file=io.BytesIO(file_bytes),
                config=types.UploadFileConfig(
                    mime_type="application/pdf",
                    display_name=file_name,
                ),
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_uri(
                        file_uri=arquivo_gemini.uri,
                        mime_type="application/pdf",
                    ),
                    instrucao,
                ],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=0)
                ),
            )

        resposta_limpa = re.sub(r"```[\w]*|```", "", response.text).strip()
        try:
            dados = json.loads(resposta_limpa)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"A IA retornou resposta inválida para '{file_name}'.\n"
                f"Erro: {e}\nResposta: {resposta_limpa[:300]}"
            )

    # Remove lançamentos substituídos pelos lançamentos fixos mensais
    todos_ignorados_up = [t.upper() for t in ignorados_fatura + ignorados_extrato]
    dados = [
        d for d in dados
        if not any(ig in (d.get("estabelecimento") or "").upper() for ig in todos_ignorados_up)
    ]

    for d in dados:
        d["mes_referencia"] = mes_referencia
        d["cartao"]         = banco
        d["tipo_pagamento"] = "05"
        d["descricao"]      = ""
        d.setdefault("comprado_por", comprador_padrao)
        d["comprado_para"] = "Família"
        _corrigir_data(d, ano, mes)
        _sanitizar_registro(d)
    return dados

def processar_arquivo(file_name: str, file_bytes: bytes, mes_referencia: str, config: dict) -> tuple:
    tipo = detectar_tipo_arquivo(file_name)
    if tipo == "extrato":
        return processar_extrato(file_name, file_bytes, mes_referencia, config), "extrato"
    return processar_fatura(file_name, file_bytes, mes_referencia, config), "fatura"


def formatar_brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_mes(mes_iso: str) -> str:
    try:
        y, m, _ = mes_iso.split("-")
        nomes = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
        return f"{nomes[int(m)-1]}/{y}"
    except Exception:
        return mes_iso


def formatar_mes_mmaaaa(mes_iso: str) -> str:
    try:
        y, m, _ = mes_iso.split("-")
        return f"{m}/{y}"
    except Exception:
        return mes_iso


def _gerar_meses_opcoes(n: int = 36) -> list:
    """Retorna os últimos n meses no formato AAAA-MM-01, do mais recente ao mais antigo."""
    today = date.today()
    meses = []
    for i in range(n):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        meses.append(f"{y:04d}-{m:02d}-01")
    return meses


def tipo_label(codigo: str) -> str:
    return TIPOS_PAGAMENTO.get(str(codigo).strip(), str(codigo))


def eh_cartao(lanc: dict) -> bool:
    return str(lanc.get("tipo_pagamento", "")).strip() == COD_CARTAO


# --- Funções para Fase 1: Checklist e Histórico ---

def _get_checklist_mes(config: dict) -> dict:
    """Retorna checklist de instituições esperadas com base na configuração da família."""
    return {
        inst["chave_checklist"]: {
            "tipo": "Fatura" if inst["tipo"] == "fatura" else "Extrato",
            "processado": False,
            "arquivos": [],
        }
        for inst in config["instituicoes"]
    }

def _carregar_checklist_do_db(mes_ref: str, config: dict) -> dict:
    """Busca checklist do histórico de importação baseado em lançamentos no banco."""
    checklist = _get_checklist_mes(config)
    try:
        res = supabase.table("lancamentos").select("cartao, estabelecimento").eq("mes_referencia", mes_ref).eq("familia_id", FAMILIA_ID).execute()
        if res.data:
            for lanc in res.data:
                cartao_val = str(lanc.get("cartao", "")).upper()
                estab = str(lanc.get("estabelecimento", "")).upper()
                for chave in checklist:
                    if chave.upper() in cartao_val or chave.upper() in estab:
                        checklist[chave]["processado"] = True
    except Exception:
        pass
    return checklist

def _atualizar_checklist(arquivos: list, config: dict) -> dict:
    """Marca instituições como processadas baseado nos arquivos."""
    checklist = _get_checklist_mes(config)
    for nome, _ in arquivos:
        nome_up = nome.upper()
        for inst in config["instituicoes"]:
            if any(p.upper() in nome_up for p in inst["padroes_arquivo"]):
                chave = inst["chave_checklist"]
                checklist[chave]["processado"] = True
                checklist[chave]["arquivos"].append(nome)
                break
    return checklist

def _exibir_checklist(checklist: dict):
    """Exibe checklist de forma visual."""
    st.subheader("📋 Checklist do Mês")
    cols = st.columns(3)
    idx = 0
    for inst, info in checklist.items():
        status_icon = "✅" if info["processado"] else "⏳"
        status_text = f"Processado" if info["processado"] else "Pendente"
        with cols[idx % 3]:
            st.metric(
                f"{status_icon} {inst}",
                info["tipo"],
                f"({status_text})"
            )
        idx += 1

def _exibir_validacoes(df: pd.DataFrame, mes_ref: str, apagados: int = 0):
    """Exibe validações após processamento."""
    st.subheader("✅ Validação Pós-Processamento")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("Total de Lançamentos", len(df))
    with col2:
        st.metric("Valor Total", formatar_brl(df["valor_parcela"].astype(float).sum()))
    with col3:
        nao_categ = len(df[df.get("categoria", "") == "Outros"]) if "categoria" in df.columns else 0
        st.metric("Sem Categoria", nao_categ, delta=f"-{nao_categ}" if nao_categ > 0 else "✓")
    with col4:
        if apagados > 0:
            st.metric("Lançamentos Removidos", apagados)

    # Resumo por instituição/cartão
    st.markdown("**Resumo por Instituição:**")
    if "cartao" in df.columns:
        resumo_cartao = df.groupby("cartao")["valor_parcela"].agg(["count", "sum"]).round(2)
        resumo_cartao.columns = ["Qtd", "Total (R$)"]
        resumo_cartao["Total (R$)"] = resumo_cartao["Total (R$)"].apply(formatar_brl)
        st.dataframe(resumo_cartao, use_container_width=True)

    # Categorias mais usadas
    st.markdown("**Categorias Identificadas:**")
    if "categoria" in df.columns:
        top_cats = df["categoria"].value_counts().head(10)
        st.bar_chart(top_cats, use_container_width=True)

def _registrar_importacao_historico(mes_ref: str, total_lanc: int, instituicoes: list, status: str):
    """Registra importação no histórico (session_state)."""
    if "importacoes_historico" not in st.session_state:
        st.session_state.importacoes_historico = []

    st.session_state.importacoes_historico.append({
        "mes": mes_ref,
        "data": pd.Timestamp.now().strftime("%d/%m/%Y %H:%M"),
        "total_lancamentos": total_lanc,
        "instituicoes": ", ".join(instituicoes),
        "status": status,
    })

def _exibir_historico():
    """Exibe histórico de importações."""
    st.subheader("📜 Histórico de Importações")

    if "importacoes_historico" not in st.session_state or not st.session_state.importacoes_historico:
        st.info("Nenhuma importação registrada ainda.")
        return

    df_hist = pd.DataFrame(st.session_state.importacoes_historico[::-1])
    st.dataframe(df_hist, use_container_width=True, hide_index=True)


def _tela_login():
    """Renderiza a tela de login/cadastro. Chamada quando não há sessão ativa."""
    st.title("🛡️ Sentinela Financeira")
    st.markdown("---")
    aba_login = st.tabs(["🔑 Entrar", "📝 Cadastrar"])

    with aba_login[0]:
        with st.form("form_login"):
            email_l = st.text_input("E-mail")
            senha_l = st.text_input("Senha", type="password")
            ok_l    = st.form_submit_button("Entrar", use_container_width=True)
        if ok_l:
            try:
                resp = supabase.auth.sign_in_with_password({"email": email_l, "password": senha_l})
                st.session_state["sb_access_token"]  = resp.session.access_token
                st.session_state["sb_refresh_token"] = resp.session.refresh_token
                # Usa RPC com SECURITY DEFINER — não depende do token no PostgREST
                _fam = supabase.rpc("get_familia_id_by_user", {"p_user_id": str(resp.user.id)}).execute()
                if not _fam.data:
                    raise Exception("Nenhuma família vinculada a este usuário.")
                st.session_state["familia_id"] = _fam.data
                st.cache_data.clear()  # Garante que config seja recarregada com dados atuais
                st.rerun()
            except Exception as e:
                st.error(f"Erro: {type(e).__name__}: {e}")

    with aba_login[1]:
        st.caption("Crie sua conta de acesso. Após o cadastro, envie seu UID ao administrador da família para ser autorizado.")
        with st.form("form_cadastro"):
            email_c  = st.text_input("E-mail")
            senha_c  = st.text_input("Senha", type="password")
            senha_c2 = st.text_input("Confirmar senha", type="password")
            ok_c     = st.form_submit_button("Criar conta", use_container_width=True)
        if ok_c:
            if not all([email_c, senha_c]):
                st.error("Preencha e-mail e senha.")
            elif senha_c != senha_c2:
                st.error("As senhas não coincidem.")
            else:
                try:
                    resp_c  = supabase.auth.sign_up({"email": email_c, "password": senha_c})
                    user_id = resp_c.user.id
                    st.success("Conta criada com sucesso!")
                    st.info(
                        f"**Seu User UID:** `{user_id}`\n\n"
                        "Envie este código ao administrador da família. "
                        "Ele vai adicioná-lo em **Configurações → Usuários Autorizados**.\n\n"
                        "Depois faça login na aba **Entrar**."
                    )
                except Exception as e:
                    st.error(f"Erro no cadastro: {e}")


# --- 3b. FUNÇÕES GOOGLE ---

def google_iniciar_oauth() -> str:
    """Salva estado da sessão e retorna URL de autenticação Google."""
    import secrets as _sec
    from google_auth_oauthlib.flow import Flow

    _OAUTH_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_id = _sec.token_urlsafe(16)
    (_OAUTH_STATE_DIR / f"{state_id}.json").write_text(json.dumps({
        "sb_access_token": st.session_state.get("sb_access_token", ""),
        "sb_refresh_token": st.session_state.get("sb_refresh_token", ""),
        "familia_id":       st.session_state.get("familia_id", ""),
        "modulo_origem":    st.session_state.get("modulo_ativo", "📅 Agenda"),
    }))
    flow = Flow.from_client_secrets_file(
        _GOOGLE_CREDS_FILE, scopes=_GOOGLE_SCOPES, redirect_uri=_GOOGLE_REDIRECT,
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent", state=state_id,
    )
    return auth_url


def google_get_credentials():
    """Carrega credenciais Google; renova token se expirado. Retorna None se não conectado."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from datetime import datetime as _dtt

    token_data = st.session_state.get("google_token")
    if not token_data and os.path.exists(_GOOGLE_TOKEN_FILE):
        with open(_GOOGLE_TOKEN_FILE) as f:
            token_data = json.load(f)
        st.session_state["google_token"] = token_data
    if not token_data:
        return None

    expiry = None
    if token_data.get("expiry"):
        try:
            expiry = _dtt.fromisoformat(token_data["expiry"])
        except Exception:
            pass

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes"),
        expiry=expiry,
    )
    if creds.expired and creds.refresh_token:
        from google.auth.exceptions import RefreshError
        try:
            creds.refresh(Request())
        except RefreshError:
            # Refresh token expirado/revogado pelo Google — descarta e força reconexão
            st.session_state.pop("google_token", None)
            if os.path.exists(_GOOGLE_TOKEN_FILE):
                os.remove(_GOOGLE_TOKEN_FILE)
            st.session_state["google_oauth_error"] = (
                "Sua conexão com o Google expirou ou foi revogada. Conecte novamente."
            )
            return None
        token_data.update(token=creds.token, expiry=creds.expiry.isoformat() if creds.expiry else None)
        st.session_state["google_token"] = token_data
        with open(_GOOGLE_TOKEN_FILE, "w") as f:
            json.dump(token_data, f)
    return creds


def google_listar_eventos(creds, dias: int = 7) -> list:
    """Retorna eventos do Google Calendar primário nos próximos `dias` dias."""
    from googleapiclient.discovery import build
    from datetime import datetime as _dtt, timezone as _tz, timedelta as _td

    service = build("calendar", "v3", credentials=creds)
    now = _dtt.now(_tz.utc)
    result = service.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=(now + _td(days=dias)).isoformat(),
        maxResults=50,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


_PALAVRAS_EMAIL_FINANCEIRO = [
    "boleto", "fatura", "vencimento", "vencendo", "pagamento", "cobrança", "cobranca",
    "débito", "debito", "fatura disponível", "invoice", "nota fiscal", "recibo", "comprovante",
]


def _email_eh_financeiro(assunto: str, resumo: str) -> bool:
    """Heurística simples: assunto ou resumo contém palavra-chave de cobrança/fatura."""
    texto = unicodedata.normalize("NFKD", f"{assunto} {resumo}".lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return any(p in texto for p in _PALAVRAS_EMAIL_FINANCEIRO)


def _email_eh_muito_importante(remetente: str, regras: list) -> bool:
    """Verifica se o remetente bate com algum critério (domínio ou e-mail) de algum grupo ativo."""
    _match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", remetente or "")
    if not _match:
        return False
    _email = _match.group(0).lower()
    _dominio = _email.split("@", 1)[1]
    for _regra in regras:
        if not _regra.get("ativo", True):
            continue
        for _crit in _regra.get("criterios", []):
            _valor = (_crit.get("valor") or "").lower()
            _tipo = _crit.get("tipo")
            if _tipo == "dominio" and (_dominio == _valor or _dominio.endswith("." + _valor)):
                return True
            if _tipo == "email" and _email == _valor:
                return True
    return False


def db_listar_regras_email_importante(familia_id: str) -> list:
    res = (
        supabase.table("regras_email_importante")
        .select("*")
        .eq("familia_id", familia_id)
        .order("nome")
        .execute()
    )
    return res.data


def db_criar_regra_email_importante(familia_id: str, nome: str, criterios: list):
    supabase.table("regras_email_importante").insert({
        "familia_id": familia_id, "nome": nome, "criterios": criterios,
    }).execute()


def db_excluir_regra_email_importante(regra_id: str):
    supabase.table("regras_email_importante").delete().eq("id", regra_id).execute()


def google_listar_labels(creds) -> list:
    """Retorna os labels do Gmail do usuário (sistema + criados por ele)."""
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=creds)
    result = service.users().labels().list(userId="me").execute()
    return result.get("labels", [])


def google_listar_emails(creds, max_results: int = 20, label_id: str = "INBOX", query: str = "") -> list:
    """Retorna metadados (assunto, remetente, data, resumo) dos e-mails mais recentes de um label."""
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=creds)
    params = {"userId": "me", "maxResults": max_results}
    if label_id:
        params["labelIds"] = [label_id]
    if query:
        params["q"] = query
    result = service.users().messages().list(**params).execute()

    emails = []
    for _msg_ref in result.get("messages", []):
        _msg = service.users().messages().get(
            userId="me", id=_msg_ref["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()
        _headers = {h["name"]: h["value"] for h in _msg.get("payload", {}).get("headers", [])}
        emails.append({
            "id":        _msg_ref["id"],
            "assunto":   _headers.get("Subject", "(sem assunto)"),
            "remetente": _headers.get("From", ""),
            "data":      _headers.get("Date", ""),
            "resumo":    _msg.get("snippet", ""),
        })
    return emails


# --- 3c. FUNÇÕES Z-API (WhatsApp) ---

def zapi_carregar_config(familia_id: str) -> dict:
    if "zapi_config" in st.session_state:
        return st.session_state["zapi_config"]
    try:
        res = (
            supabase.table("integracoes_familia")
            .select("config")
            .eq("familia_id", familia_id)
            .eq("tipo", "zapi")
            .single()
            .execute()
        )
        cfg = res.data["config"] if res.data else {}
    except Exception:
        cfg = {}
    st.session_state["zapi_config"] = cfg
    return cfg


def zapi_salvar_config(familia_id: str, config: dict):
    supabase.table("integracoes_familia").upsert(
        {"familia_id": familia_id, "tipo": "zapi", "config": config},
        on_conflict="familia_id,tipo",
    ).execute()
    st.session_state["zapi_config"] = config


def zapi_status(instance_id: str, token: str) -> str:
    import requests as _req
    try:
        url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/status"
        r = _req.get(url, timeout=8)
        return r.json().get("value", "unknown") if r.status_code == 200 else "error"
    except Exception:
        return "error"


def zapi_enviar_mensagem(instance_id: str, token: str, telefone: str, mensagem: str) -> bool:
    import requests as _req
    try:
        url = f"https://api.z-api.io/instances/{instance_id}/token/{token}/send-text"
        r = _req.post(url, json={"phone": telefone, "message": mensagem}, timeout=10)
        return r.status_code == 200
    except Exception:
        return False


# --- 4. INTERFACE ---

st.set_page_config(page_title="Controle Familiar", layout="wide", page_icon="🏠")

# --- Autenticação ---
_session = supabase.auth.get_session()
if not _session:
    _tela_login()
    st.stop()

# Resolve FAMILIA_ID para o usuário logado
if "familia_id" not in st.session_state:
    try:
        _fam_res = supabase.table("familias").select("id").eq("auth_user_id", _session.user.id).single().execute()
        st.session_state["familia_id"] = _fam_res.data["id"]
    except Exception:
        st.error("Sua conta ainda não está vinculada a uma família. Execute o UPDATE no Supabase conforme instruções em auth_rls.sql.")
        if st.button("🚪 Sair"):
            supabase.auth.sign_out()
            for _k in ["sb_access_token", "sb_refresh_token", "familia_id"]:
                st.session_state.pop(_k, None)
            st.rerun()
        st.stop()

FAMILIA_ID = st.session_state["familia_id"]

with st.sidebar:
    st.markdown("# 🏠 Controle Familiar")
    st.divider()
    modulo = st.radio(
        "Módulo",
        ["💰 Financeiro", "📅 Agenda", "📧 E-mails", "⚙️ Configurações"],
        key="modulo_ativo",
        label_visibility="collapsed",
    )
    st.divider()
    st.caption(f"👤 {_session.user.email}")
    if st.button("🚪 Sair", key="btn_logout"):
        supabase.auth.sign_out()
        st.cache_data.clear()
        for _k in ["sb_access_token", "sb_refresh_token", "familia_id"]:
            st.session_state.pop(_k, None)
        st.rerun()

_TITULOS = {
    "💰 Financeiro": "🛡️ Sentinela Financeira",
    "📅 Agenda":     "📅 Agenda",
    "📧 E-mails":    "📧 E-mails",
    "⚙️ Configurações": "⚙️ Configurações do Sistema",
}
st.title(_TITULOS[modulo])

# Carrega configuração da família (cache 5 min)
_config = buscar_config_familia(FAMILIA_ID)
_nomes_membros    = [m["nome"] for m in _config["membros"]]
_compradores_para = _nomes_membros + ["Família"]

# =============================================================
# MÓDULOS NÃO-FINANCEIROS — stubs até implementação completa
# =============================================================
if modulo == "📅 Agenda":
    if "google_oauth_error" in st.session_state:
        st.error(f"Erro ao conectar Google: {st.session_state.pop('google_oauth_error')}")

    if st.session_state.pop("google_conectado", False):
        st.success("Google Calendar conectado com sucesso!")

    _gcreds = google_get_credentials()

    if _gcreds is None:
        st.info("Conecte sua conta Google para visualizar os eventos da agenda.")
        _col1, _col2 = st.columns([1, 2])
        with _col1:
            if os.path.exists(_GOOGLE_CREDS_FILE):
                _auth_url = google_iniciar_oauth()
                st.link_button("🔗 Conectar Google Calendar", url=_auth_url, type="primary")
            else:
                st.warning("Arquivo `google_credentials.json` não encontrado na pasta do projeto.")
        with _col2:
            st.markdown("""
**Como funciona:**
1. Clique no botão ao lado
2. Faça login na sua conta Google
3. Autorize o acesso à Agenda
4. Você voltará aqui automaticamente com os eventos carregados
            """)
    else:
        _col_per, _ = st.columns([3, 5])
        with _col_per:
            _dias = st.radio("Período", [7, 14, 30], index=0,
                             format_func=lambda x: f"Próximos {x} dias", horizontal=True)

        with st.spinner("Carregando eventos..."):
            try:
                _eventos = google_listar_eventos(_gcreds, dias=_dias)
            except Exception as _ex:
                st.error(f"Erro ao carregar eventos: {_ex}")
                _eventos = []

        _zapi_cfg_ag = zapi_carregar_config(FAMILIA_ID)
        _zapi_ok = bool(_zapi_cfg_ag.get("instance_id") and _zapi_cfg_ag.get("telefone"))

        if not _eventos:
            st.info(f"Nenhum evento nos próximos {_dias} dias.")
        else:
            st.caption(f"{len(_eventos)} evento(s) encontrado(s)")
            for _i_ev, _ev in enumerate(_eventos):
                _inicio_raw = _ev["start"].get("dateTime", _ev["start"].get("date", ""))
                _titulo = _ev.get("summary", "Sem título")
                _local  = _ev.get("location", "")
                _desc   = (_ev.get("description") or "").strip()
                try:
                    from datetime import datetime as _dtt, date as _date_t
                    if "T" in _inicio_raw:
                        _dt_ev = _dtt.fromisoformat(_inicio_raw)
                        _data_fmt = _dt_ev.strftime("%d/%m  %H:%M")
                    else:
                        _dt_ev = _date_t.fromisoformat(_inicio_raw)
                        _data_fmt = _dt_ev.strftime("%d/%m") + "  dia todo"
                except Exception:
                    _data_fmt = _inicio_raw

                with st.container(border=True):
                    _c1, _c2, _c3 = st.columns([4, 1, 1])
                    with _c1:
                        st.markdown(f"**{_titulo}**")
                        if _local:
                            st.caption(f"📍 {_local}")
                        if _desc:
                            st.caption(_desc[:150] + ("…" if len(_desc) > 150 else ""))
                    with _c2:
                        st.markdown(f"`{_data_fmt}`")
                    with _c3:
                        if _zapi_ok:
                            if st.button("📲", key=f"zapi_ev_{_i_ev}", help="Enviar lembrete via WhatsApp"):
                                _msg_ev = f"📅 *{_titulo}*\n🗓️ {_data_fmt}"
                                if _local:
                                    _msg_ev += f"\n📍 {_local}"
                                if _desc:
                                    _msg_ev += f"\n\n{_desc[:200]}"
                                with st.spinner("Enviando..."):
                                    _ok_ev = zapi_enviar_mensagem(
                                        _zapi_cfg_ag["instance_id"], _zapi_cfg_ag["token"],
                                        _zapi_cfg_ag["telefone"], _msg_ev,
                                    )
                                if _ok_ev:
                                    st.toast("Lembrete enviado! ✅")
                                else:
                                    st.toast("Falha ao enviar ❌")

        st.divider()
        if st.button("🔌 Desconectar Google", type="secondary"):
            if os.path.exists(_GOOGLE_TOKEN_FILE):
                os.remove(_GOOGLE_TOKEN_FILE)
            st.session_state.pop("google_token", None)
            st.rerun()

    st.stop()

if modulo == "📧 E-mails":
    if "google_oauth_error" in st.session_state:
        st.error(f"Erro ao conectar Google: {st.session_state.pop('google_oauth_error')}")

    if st.session_state.pop("google_conectado", False):
        st.success("Google conectado com sucesso!")

    _gcreds_mail = google_get_credentials()

    if _gcreds_mail is None:
        st.info("Conecte sua conta Google para visualizar seus e-mails.")
        _col1, _col2 = st.columns([1, 2])
        with _col1:
            if os.path.exists(_GOOGLE_CREDS_FILE):
                _auth_url_mail = google_iniciar_oauth()
                st.link_button("🔗 Conectar Gmail", url=_auth_url_mail, type="primary")
            else:
                st.warning("Arquivo `google_credentials.json` não encontrado na pasta do projeto.")
        with _col2:
            st.markdown("""
**Como funciona:**
1. Clique no botão ao lado
2. Faça login na sua conta Google
3. Autorize o acesso ao Gmail
4. Você voltará aqui automaticamente com os e-mails carregados
            """)
    else:
        try:
            _labels_gmail = google_listar_labels(_gcreds_mail)
            _opcoes_label = {"Caixa de entrada": "INBOX"}
            for _lbl in _labels_gmail:
                if _lbl.get("type") == "user":
                    _opcoes_label[_lbl["name"]] = _lbl["id"]
        except Exception as _ex_lbl:
            st.error(f"Erro ao carregar labels: {_ex_lbl}")
            _opcoes_label = {"Caixa de entrada": "INBOX"}

        _c_lbl, _c_busca = st.columns([2, 3])
        with _c_lbl:
            _label_nome = st.selectbox("Label", list(_opcoes_label.keys()))
        with _c_busca:
            _busca_email = st.text_input("Buscar", placeholder="ex: assunto, remetente, palavra-chave...")

        _c_fin, _c_imp = st.columns(2)
        with _c_fin:
            _so_financeiros = st.checkbox("💸 Somente boletos/faturas", value=False)
        with _c_imp:
            _so_importantes = st.checkbox("⭐ Somente muito importantes", value=False)

        _regras_importantes = db_listar_regras_email_importante(FAMILIA_ID)
        with st.expander("⚙️ Gerenciar regras de importância (⭐)"):
            st.caption(
                "Crie grupos de regras para marcar automaticamente e-mails de remetentes "
                "específicos ou domínios como **muito importantes** (ex: e-mails do trabalho)."
            )
            if _regras_importantes:
                for _r in _regras_importantes:
                    with st.container(border=True):
                        _rc1, _rc2, _rc3 = st.columns([3, 5, 1])
                        with _rc1:
                            st.markdown(f"**⭐ {_r['nome']}**")
                        with _rc2:
                            _doms = [c["valor"] for c in _r["criterios"] if c.get("tipo") == "dominio"]
                            _ems  = [c["valor"] for c in _r["criterios"] if c.get("tipo") == "email"]
                            _partes = []
                            if _doms:
                                _partes.append("domínio: " + ", ".join(_doms))
                            if _ems:
                                _partes.append("e-mail: " + ", ".join(_ems))
                            st.caption(" · ".join(_partes) if _partes else "(sem critérios)")
                        with _rc3:
                            if st.button("🗑️", key=f"del_regra_imp_{_r['id']}", help="Excluir grupo"):
                                db_excluir_regra_email_importante(_r["id"])
                                st.rerun()
                st.divider()

            st.markdown("**Novo grupo**")
            with st.form("form_nova_regra_email_importante", clear_on_submit=True):
                _nome_grupo = st.text_input("Nome do grupo", placeholder="ex: Trabalho - MundoTelecom")
                _fc1, _fc2 = st.columns(2)
                with _fc1:
                    _dominios_txt = st.text_area(
                        "Domínios (um por linha)", placeholder="mundotelecom.com.br",
                        help="Marca como importante qualquer remetente desse domínio (ou subdomínio).",
                    )
                with _fc2:
                    _emails_txt = st.text_area(
                        "E-mails específicos (um por linha)", placeholder="fulano@empresa.com",
                    )
                if st.form_submit_button("➕ Adicionar grupo"):
                    _criterios = []
                    for _d in [l.strip().lstrip("@").lower() for l in _dominios_txt.splitlines() if l.strip()]:
                        _criterios.append({"tipo": "dominio", "valor": _d})
                    for _e in [l.strip().lower() for l in _emails_txt.splitlines() if l.strip()]:
                        _criterios.append({"tipo": "email", "valor": _e})
                    if _nome_grupo and _criterios:
                        db_criar_regra_email_importante(FAMILIA_ID, _nome_grupo, _criterios)
                        st.success("Grupo criado!")
                        st.rerun()
                    else:
                        st.warning("Informe um nome e ao menos um domínio ou e-mail.")

        with st.spinner("Carregando e-mails..."):
            try:
                _emails = google_listar_emails(
                    _gcreds_mail, max_results=30,
                    label_id=_opcoes_label[_label_nome], query=_busca_email,
                )
            except Exception as _ex_mail:
                st.error(f"Erro ao carregar e-mails: {_ex_mail}")
                _emails = []

        if _so_financeiros:
            _emails = [e for e in _emails if _email_eh_financeiro(e["assunto"], e["resumo"])]
        if _so_importantes:
            _emails = [e for e in _emails if _email_eh_muito_importante(e["remetente"], _regras_importantes)]

        _zapi_cfg_mail = zapi_carregar_config(FAMILIA_ID)
        _zapi_ok_mail = bool(_zapi_cfg_mail.get("instance_id") and _zapi_cfg_mail.get("telefone"))

        if not _emails:
            st.info("Nenhum e-mail encontrado para os filtros selecionados.")
        else:
            st.caption(f"{len(_emails)} e-mail(s) encontrado(s)")
            for _i_em, _em in enumerate(_emails):
                try:
                    from email.utils import parsedate_to_datetime as _parsedate
                    _data_fmt_em = _parsedate(_em["data"]).strftime("%d/%m  %H:%M")
                except Exception:
                    _data_fmt_em = _em["data"][:16]

                _financeiro = _email_eh_financeiro(_em["assunto"], _em["resumo"])
                _importante = _email_eh_muito_importante(_em["remetente"], _regras_importantes)

                with st.container(border=True):
                    _ce1, _ce2, _ce3 = st.columns([4, 1, 1])
                    with _ce1:
                        _badge = ("⭐ " if _importante else "") + ("💸 " if _financeiro else "")
                        st.markdown(f"**{_badge}{_em['assunto']}**")
                        st.caption(f"De: {_em['remetente']}")
                        if _em["resumo"]:
                            st.caption(_em["resumo"][:150] + ("…" if len(_em["resumo"]) > 150 else ""))
                    with _ce2:
                        st.markdown(f"`{_data_fmt_em}`")
                    with _ce3:
                        if _zapi_ok_mail:
                            if st.button("📲", key=f"zapi_email_{_i_em}", help="Encaminhar para WhatsApp"):
                                _msg_em = (
                                    f"📧 *{_em['assunto']}*\n"
                                    f"De: {_em['remetente']}\n\n"
                                    f"{_em['resumo'][:300]}"
                                )
                                with st.spinner("Enviando..."):
                                    _ok_em = zapi_enviar_mensagem(
                                        _zapi_cfg_mail["instance_id"], _zapi_cfg_mail["token"],
                                        _zapi_cfg_mail["telefone"], _msg_em,
                                    )
                                if _ok_em:
                                    st.toast("E-mail encaminhado! ✅")
                                else:
                                    st.toast("Falha ao enviar ❌")

        st.divider()
        if st.button("🔌 Desconectar Google", type="secondary", key="btn_desconectar_google_email"):
            if os.path.exists(_GOOGLE_TOKEN_FILE):
                os.remove(_GOOGLE_TOKEN_FILE)
            st.session_state.pop("google_token", None)
            st.rerun()

    st.stop()

if modulo == "⚙️ Configurações":
    _tab_zapi, _tab_google = st.tabs(["📱 WhatsApp (Z-API)", "🔗 Google"])

    # ── Aba Z-API ──────────────────────────────────────────────
    with _tab_zapi:
        _zapi_cfg = zapi_carregar_config(FAMILIA_ID)

        with st.expander("ℹ️ Como obter as credenciais Z-API", expanded=not bool(_zapi_cfg.get("instance_id"))):
            st.markdown("""
1. Acesse **[z-api.io](https://z-api.io)** e crie uma conta gratuita
2. Crie uma nova instância → você receberá o **Instance ID** e o **Token**
3. Cole abaixo, salve, e use o QR Code para conectar seu WhatsApp
            """)

        with st.form("form_zapi_config"):
            _c1, _c2 = st.columns(2)
            with _c1:
                _inst = st.text_input("Instance ID", value=_zapi_cfg.get("instance_id", ""),
                                      placeholder="ex: 3B0B1234ABCD")
            with _c2:
                _tok = st.text_input("Token", value=_zapi_cfg.get("token", ""),
                                     type="password", placeholder="ex: ABC123...")
            _fone = st.text_input(
                "Telefone para notificações",
                value=_zapi_cfg.get("telefone", ""),
                placeholder="5511999999999  (código país + DDD + número, sem espaços)",
            )
            if st.form_submit_button("💾 Salvar configuração"):
                if _inst and _tok and _fone:
                    zapi_salvar_config(FAMILIA_ID, {
                        "instance_id": _inst, "token": _tok, "telefone": _fone,
                    })
                    st.success("Configuração salva!")
                    st.rerun()
                else:
                    st.warning("Preencha todos os campos.")

        if _zapi_cfg.get("instance_id"):
            st.divider()
            with st.spinner("Verificando conexão..."):
                _wstatus = zapi_status(_zapi_cfg["instance_id"], _zapi_cfg["token"])

            if _wstatus in ("connected", "CONNECTED"):
                st.success("✅ WhatsApp conectado")
                _col_test, _ = st.columns([2, 3])
                with _col_test:
                    if st.button("📲 Enviar mensagem de teste"):
                        with st.spinner("Enviando..."):
                            _ok = zapi_enviar_mensagem(
                                _zapi_cfg["instance_id"], _zapi_cfg["token"],
                                _zapi_cfg["telefone"],
                                "✅ *Controle Familiar* conectado com sucesso!\n\nVocê receberá notificações aqui.",
                            )
                        if _ok:
                            st.success("Mensagem enviada! Verifique seu WhatsApp.")
                        else:
                            st.error("Falha ao enviar. Verifique as credenciais.")
            else:
                st.warning(f"Status: **{_wstatus}** — escaneie o QR Code abaixo com o WhatsApp para conectar.")
                _qr_url = (
                    f"https://api.z-api.io/instances/{_zapi_cfg['instance_id']}"
                    f"/token/{_zapi_cfg['token']}/qr-code/image"
                )
                st.image(_qr_url, width=260, caption="Abra o WhatsApp → Dispositivos conectados → Conectar dispositivo")

    # ── Aba Google ─────────────────────────────────────────────
    with _tab_google:
        _gcreds2 = google_get_credentials()
        if _gcreds2 is not None:
            st.success("✅ Google conectado")
            if st.button("🔌 Desconectar Google", key="btn_desc_google_cfg"):
                if os.path.exists(_GOOGLE_TOKEN_FILE):
                    os.remove(_GOOGLE_TOKEN_FILE)
                st.session_state.pop("google_token", None)
                st.rerun()
        else:
            st.info("Google não conectado. Vá ao módulo **📅 Agenda** para conectar.")

    st.stop()

# =============================================================
# MÓDULO FINANCEIRO — navegação original
# =============================================================
pagina = st.sidebar.selectbox(
    "Navegação",
    ["📄 Processar Arquivos", "💳 Gerenciar Lançamentos", "📊 Relatórios",
     "📂 Categorias", "🏷️ Regras de Classificação", "⚙️ Configurações",
     "🔧 Manutenção"],
)

# =============================================================
# PÁGINA 1 — PROCESSAR ARQUIVOS
# =============================================================
if pagina == "📄 Processar Arquivos":

    if "df_final" not in st.session_state:
        st.session_state.df_final = None
    if "checklist_mes" not in st.session_state:
        st.session_state.checklist_mes = None
    if "apagados_pre" not in st.session_state:
        st.session_state.apagados_pre = 0

    abas_proc = st.tabs(["📥 Importar Arquivos", "📜 Histórico"])

    with abas_proc[0]:
        _opcoes_mes_proc = _gerar_meses_opcoes()
        mes_referencia_str = st.selectbox(
            "Mês de referência",
            options=_opcoes_mes_proc,
            format_func=formatar_mes_mmaaaa,
            key="proc_mes_ref",
        )
        mes_ref_input = formatar_mes_mmaaaa(mes_referencia_str)
        if mes_referencia_str:
            # Exibe checklist inicial para o mês
            if st.session_state.checklist_mes is None or st.session_state.checklist_mes.get("mes") != mes_referencia_str:
                checklist_db = _carregar_checklist_do_db(mes_referencia_str, _config)
                st.session_state.checklist_mes = {"mes": mes_referencia_str, "checklist": checklist_db}
            _exibir_checklist(st.session_state.checklist_mes["checklist"])

        files = st.file_uploader(
            "Arraste faturas de cartão e/ou extratos bancários (PDF)",
            type="pdf", accept_multiple_files=True,
            help="Faturas: BB, C6, Múltiplo, Visa Infinity. Extratos: nome deve conter 'Extrato'.",
            disabled=(mes_referencia_str is None),
        )

        # Atualiza checklist quando arquivos são selecionados
        if files and st.session_state.checklist_mes is not None:
            arquivos_nomes = [(f.name, f.getvalue()) for f in files]
            st.session_state.checklist_mes["checklist"] = _atualizar_checklist(arquivos_nomes, _config)

        if files and st.button("🚀 Iniciar Processamento", disabled=(mes_referencia_str is None)):
            st.session_state.df_final = None
            regras      = buscar_regras_db()
            consolidado = []
            arquivos    = [(f.name, f.getvalue()) for f in files]
            progress    = st.progress(0, text="Iniciando…")
            status      = st.empty()
            total       = len(arquivos)
            erros       = []

            # Determina quais cartões/bancos estão sendo processados nesta rodada
            cartoes_a_processar = set()
            tem_extrato_itau = False
            itau_inst = next((i for i in _config["instituicoes"] if i["tipo"] == "extrato" and any("ITAU" in p.upper() or "ITAÚ" in p.upper() for p in i["padroes_arquivo"])), None)
            for nome_arq, _ in arquivos:
                if detectar_tipo_arquivo(nome_arq) == "extrato":
                    cartoes_a_processar.add(banco_do_arquivo(nome_arq, _config))
                    if itau_inst and any(p.upper() in nome_arq.upper() for p in itau_inst["padroes_arquivo"]):
                        tem_extrato_itau = True
                else:
                    cartoes_a_processar.add(cartao_do_arquivo(nome_arq, _config))
            if tem_extrato_itau:
                cartoes_a_processar.add(itau_inst["nome"] if itau_inst else "Itaú")

            # Apaga SOMENTE os lançamentos das instituições sendo reprocessadas, preservando os demais
            try:
                res_del = (supabase.table("lancamentos")
                           .delete()
                           .eq("mes_referencia", mes_referencia_str)
                           .eq("familia_id", FAMILIA_ID)
                           .in_("cartao", list(cartoes_a_processar))
                           .execute())
                apagados_pre = len(res_del.data) if res_del.data else 0
                st.session_state.apagados_pre = apagados_pre
                inst_str = ", ".join(sorted(cartoes_a_processar))
                if apagados_pre:
                    status.info(f"🗑️ {apagados_pre} lançamento(s) de [{inst_str}] removido(s) antes do reprocessamento.")
            except Exception as e:
                status.warning(f"⚠️ Não foi possível limpar lançamentos anteriores: {e}")

            with ThreadPoolExecutor(max_workers=min(total, 2)) as executor:
                futuros = {
                    executor.submit(processar_arquivo, nome, dados, mes_referencia_str, _config): nome
                    for nome, dados in arquivos
                }
                concluidos = 0
                for futuro in as_completed(futuros):
                    nome = futuros[futuro]
                    concluidos += 1
                    try:
                        resultado, tipo_arq = futuro.result()
                        consolidado.extend(resultado)
                        icone = "🏦" if tipo_arq == "extrato" else "💳"
                        if resultado:
                            status.success(f"{icone} {nome} — {len(resultado)} lançamento(s)")
                        else:
                            status.warning(f"⚠️ {nome} — 0 lançamentos extraídos (verifique se o PDF tem texto selecionável)")
                    except json.JSONDecodeError as e:
                        erros.append(nome); status.error(f"Erro JSON em {nome}: {e}")
                    except Exception as e:
                        erros.append(nome); status.error(f"❌ Erro em {nome}: {e}")
                    progress.progress(concluidos / total, text=f"{concluidos}/{total} processados")

            progress.empty()

            # Adiciona lançamentos fixos mensais automaticamente
            import calendar as _cal
            ano_ref, mes_ref_num, _ = mes_referencia_str.split("-")
            ultimo_dia = _cal.monthrange(int(ano_ref), int(mes_ref_num))[1]
            data_fixo  = f"{ano_ref}-{mes_ref_num}-{ultimo_dia:02d}"

            fixos_a_incluir = []
            if tem_extrato_itau:
                fixos_a_incluir = [f for f in _config["fixos"] if f.get("gatilho") == "extrato_itau"]
            else:
                fixos_a_incluir = [f for f in _config["fixos"] if f.get("gatilho") == "sempre"]

            for lf in fixos_a_incluir:
                fixo = {k: v for k, v in lf.items() if k not in ("id", "familia_id", "gatilho", "created_at")}
                fixo["data_origem"]    = data_fixo
                fixo["mes_referencia"] = mes_referencia_str
                _sanitizar_registro(fixo)
                consolidado.append(fixo)
            status.info(f"📌 {len(fixos_a_incluir)} lançamento(s) fixo(s) adicionado(s)")

            if consolidado:
                df_final = pd.DataFrame(consolidado)
                df_final = aplicar_categorias(df_final, regras, _config.get("categorias_padrao"))
                df_final["_tipo_label"] = df_final["tipo_pagamento"].apply(tipo_label)
                st.session_state.df_final = df_final
            if erros:
                st.warning(f"⚠️ {len(erros)} arquivo(s) com erro: {', '.join(erros)}")

        if st.session_state.df_final is not None:
            df = st.session_state.df_final
            st.subheader("📋 Lançamentos processados")
            cols_rel = ["data_origem", "estabelecimento", "descricao", "cartao",
                        "comprado_por", "comprado_para", "valor_parcela", "_tipo_label", "categoria", "subcategoria"]
            cols_ex  = [c for c in cols_rel if c in df.columns]
            st.dataframe(
                df[cols_ex].rename(columns={"_tipo_label": "tipo de pagamento"}),
                use_container_width=True, hide_index=True,
            )
            st.metric("Total", formatar_brl(df["valor_parcela"].astype(float).sum()))

            # Exibe validações pós-processamento
            _exibir_validacoes(df, mes_ref_input, apagados=st.session_state.apagados_pre)

            if st.button("💾 Salvar no Supabase"):
                try:
                    import math, numpy as _np
                    cols_salvar = [c for c in df.columns if c != "_tipo_label"]
                    registros   = df[cols_salvar].to_dict(orient="records")
                    # Sanitiza NaN/Inf/numpy e adiciona familia_id antes de enviar ao Supabase
                    for r in registros:
                        r["familia_id"] = FAMILIA_ID
                        for k, v in list(r.items()):
                            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                                r[k] = 0.0
                            elif isinstance(v, _np.floating):
                                r[k] = float(v) if math.isfinite(float(v)) else 0.0
                            elif isinstance(v, _np.integer):
                                r[k] = int(v)
                            elif v != v:
                                r[k] = None

                    # DELETE de segurança: remove qualquer dado residual dos mesmos cartões
                    # antes de inserir — garante que reimportações não causem duplicação
                    _cartoes_para_salvar = list({
                        r["cartao"] for r in registros if r.get("cartao")
                    })
                    if _cartoes_para_salvar:
                        supabase.table("lancamentos").delete()\
                            .eq("mes_referencia", mes_referencia_str)\
                            .eq("familia_id", FAMILIA_ID)\
                            .in_("cartao", _cartoes_para_salvar)\
                            .execute()

                    supabase.table("lancamentos").insert(registros).execute()
                    st.balloons()
                    st.success("Dados gravados com sucesso!")

                    # Registra no histórico com contagem real do banco
                    instituicoes_proc = []
                    if st.session_state.checklist_mes is not None:
                        for inst, info in st.session_state.checklist_mes["checklist"].items():
                            if info.get("processado"):
                                instituicoes_proc.append(inst)

                    # Busca contagem real de lançamentos salvos no banco
                    try:
                        res_count = supabase.table("lancamentos").select("id", count="exact").eq("mes_referencia", mes_referencia_str).execute()
                        total_real = res_count.count if hasattr(res_count, 'count') else len(res_count.data) if res_count.data else 0
                    except Exception:
                        total_real = len(df)

                    _registrar_importacao_historico(mes_ref_input, total_real, instituicoes_proc, "✅ Sucesso")

                    st.session_state.df_final = None
                except Exception as e:
                    st.error(f"Erro ao salvar: {e}")

    with abas_proc[1]:
        _exibir_historico()


# =============================================================
# PÁGINA 2 — GERENCIAR LANÇAMENTOS
# =============================================================
elif pagina == "💳 Gerenciar Lançamentos":

    st.header("💳 Gerenciar Lançamentos")
    st.info(
        "Lançamentos de **cartão de crédito** são gerenciados exclusivamente pela importação de faturas. "
        "Aqui podem ser consultados, mas não incluídos, alterados ou excluídos."
    )

    aba = st.tabs(["➕ Incluir", "✏️ Alterar", "🗑️ Excluir", "📋 Listar / Alterar / Excluir em lote", "🔍 Sem Classificação", "✏️ Listar / Alterar / Excluir"])

    regras = buscar_regras_db()
    _cats_db = buscar_categorias_db()
    cats_disp = sorted(set(c["categoria"] for c in _cats_db)) if _cats_db else (sorted(set(r["categoria"] for r in regras)) + ["Outros"])
    subcats_map = {}
    for c in _cats_db:
        subcats_map.setdefault(c["categoria"], set()).add(c["subcategoria"])
    if not subcats_map:
        for r in regras:
            subcats_map.setdefault(r["categoria"], set()).add(r["subcategoria"])
    subcats_map.setdefault("Outros", {"Outros"})

    # ABA INCLUIR
    with aba[0]:
        st.subheader("Novo lançamento")
        st.caption("Apenas lançamentos de fontes que não sejam cartão de crédito.")
        with st.form("form_inc"):
            c1, c2 = st.columns(2)
            with c1:
                d_data  = st.date_input("Data", value=date.today())
                d_estab = st.text_input("Estabelecimento")
                d_desc  = st.text_input("Descrição")
                d_valor = st.number_input("Valor (R$)", min_value=0.0, format="%.2f")
                d_tipo      = st.selectbox("Tipo de pagamento", TIPOS_NAO_CARTAO)
                d_comprador = st.text_input("Comprado por")
                d_comprado_para = st.text_input("Comprado para")
            with c2:
                _opcoes_mes_inc = _gerar_meses_opcoes()
                d_mes = st.selectbox("Mês de referência", options=_opcoes_mes_inc,
                                     format_func=formatar_mes_mmaaaa, key="inc_mes_ref")
                d_cat   = st.selectbox("Categoria", cats_disp)
            d_subcats = sorted(subcats_map.get(d_cat, {"Outros"}))
            d_subcat  = st.selectbox("Subcategoria", d_subcats)
            ok_inc    = st.form_submit_button("✅ Incluir")

        if ok_inc:
            if not d_estab:
                st.warning("Estabelecimento é obrigatório.")
            else:
                try:
                    supabase.table("lancamentos").insert({
                        "data_origem": str(d_data), "estabelecimento": d_estab.strip(),
                        "descricao": d_desc.strip(), "valor_parcela": d_valor,
                        "cartao": "", "mes_referencia": d_mes,
                        "categoria": d_cat, "subcategoria": d_subcat,
                        "tipo_pagamento":  d_tipo[:2],
                        "comprado_por":    d_comprador.strip(),
                        "comprado_para":   d_comprado_para.strip(),
                        "familia_id":      FAMILIA_ID,
                    }).execute()
                    st.success("Lançamento incluído!")
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ABA ALTERAR
    with aba[1]:
        st.subheader("Alterar lançamento")
        todos     = buscar_lancamentos_db()
        editaveis = [l for l in todos if not eh_cartao(l)]

        if not todos:
            st.info("Nenhum lançamento cadastrado.")
        elif not editaveis:
            st.warning("Todos os lançamentos são de cartão de crédito e não podem ser alterados aqui.")
        else:
            opts = {f"{l['id']} — {l.get('data_origem','')} | {l.get('estabelecimento','')} | R$ {float(l.get('valor_parcela',0)):.2f}": l for l in editaveis}
            sel  = st.selectbox("Selecione", list(opts.keys()))
            lanc = opts[sel]
            with st.form("form_alt"):
                c1, c2 = st.columns(2)
                with c1:
                    a_data  = st.date_input("Data", value=date.fromisoformat(lanc["data_origem"]) if lanc.get("data_origem") else date.today())
                    a_estab = st.text_input("Estabelecimento", value=lanc.get("estabelecimento",""))
                    a_desc      = st.text_input("Descrição", value=lanc.get("descricao",""))
                    a_comprador = st.text_input("Comprado por", value=lanc.get("comprado_por",""))
                    a_comprado_para = st.text_input("Comprado para", value=lanc.get("comprado_para",""))
                    a_valor = st.number_input("Valor (R$)", value=float(lanc.get("valor_parcela",0)), format="%.2f")
                    t_atual = tipo_label(lanc.get("tipo_pagamento","02"))
                    t_idx   = TIPOS_NAO_CARTAO.index(t_atual) if t_atual in TIPOS_NAO_CARTAO else 0
                    a_tipo  = st.selectbox("Tipo de pagamento", TIPOS_NAO_CARTAO, index=t_idx)
                with c2:
                    _opcoes_mes_alt = _gerar_meses_opcoes()
                    _mes_atual = lanc.get("mes_referencia", _opcoes_mes_alt[0])
                    _mes_idx = _opcoes_mes_alt.index(_mes_atual) if _mes_atual in _opcoes_mes_alt else 0
                    a_mes = st.selectbox("Mês de referência", options=_opcoes_mes_alt,
                                         format_func=formatar_mes_mmaaaa, index=_mes_idx, key="alt_mes_ref")
                    c_idx   = cats_disp.index(lanc.get("categoria","Outros")) if lanc.get("categoria") in cats_disp else len(cats_disp)-1
                    a_cat   = st.selectbox("Categoria", cats_disp, index=c_idx)
                a_subs  = sorted(subcats_map.get(a_cat, {"Outros"}))
                s_idx   = a_subs.index(lanc.get("subcategoria","Outros")) if lanc.get("subcategoria") in a_subs else 0
                a_sub   = st.selectbox("Subcategoria", a_subs, index=s_idx)
                ok_alt  = st.form_submit_button("💾 Salvar")

            if ok_alt:
                try:
                    supabase.table("lancamentos").update({
                        "data_origem": str(a_data), "estabelecimento": a_estab.strip(),
                        "descricao": a_desc.strip(), "valor_parcela": a_valor,
                        "mes_referencia": a_mes,
                        "categoria": a_cat, "subcategoria": a_sub,
                        "tipo_pagamento": a_tipo[:2],
                        "comprado_por":   a_comprador.strip(),
                        "comprado_para":  a_comprado_para.strip(),
                    }).eq("id", lanc["id"]).execute()
                    st.success("Atualizado!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ABA EXCLUIR
    with aba[2]:
        st.subheader("Excluir lançamento")
        todos     = buscar_lancamentos_db()
        editaveis = [l for l in todos if not eh_cartao(l)]

        if not todos:
            st.info("Nenhum lançamento cadastrado.")
        elif not editaveis:
            st.warning("Todos os lançamentos são de cartão de crédito e não podem ser excluídos aqui.")
        else:
            opts_e  = {f"{l['id']} — {l.get('data_origem','')} | {l.get('estabelecimento','')} | R$ {float(l.get('valor_parcela',0)):.2f}": l for l in editaveis}
            sel_e   = st.selectbox("Selecione para excluir", list(opts_e.keys()))
            lanc_e  = opts_e[sel_e]
            st.warning(f"Excluir **{lanc_e.get('estabelecimento','')}** — R$ {float(lanc_e.get('valor_parcela',0)):.2f}?")
            if st.button("🗑️ Confirmar exclusão"):
                try:
                    supabase.table("lancamentos").delete().eq("id", lanc_e["id"]).execute()
                    st.success("Excluído!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ABA LISTAR / ALTERAR / EXCLUIR EM LOTE
    with aba[3]:
        st.subheader("Consultar, alterar ou excluir em lote")

        todos_lanc = buscar_lancamentos_db()
        if not todos_lanc:
            st.info("Nenhum lançamento cadastrado.")
        else:
            cols_all = ["id","data_origem","estabelecimento","descricao","comprado_por","comprado_para",
                        "valor_parcela","cartao","mes_referencia","tipo_pagamento",
                        "categoria","subcategoria","periodicidade","importancia","tipo_categoria"]
            cols_ex_all = [c for c in cols_all if c in todos_lanc[0]]
            df_all = pd.DataFrame(todos_lanc)[cols_ex_all]

            # --- Filtros ---
            with st.expander("🔍 Filtros", expanded=True):
                fl1, fl2, fl3, fl4 = st.columns(4)
                with fl1:
                    _fl_mes_opts = (["Todos"] + sorted(df_all["mes_referencia"].dropna().unique().tolist(), reverse=True)) if "mes_referencia" in df_all.columns else ["Todos"]
                    fl_mes = st.selectbox("Mês de referência", _fl_mes_opts, key="ll_mes",
                        format_func=lambda x: x if x == "Todos" else formatar_mes_mmaaaa(x))
                    fl_cartao = st.selectbox("Cartão / Banco", ["Todos"] + sorted(df_all["cartao"].dropna().unique().tolist()) if "cartao" in df_all.columns else ["Todos"], key="ll_cartao")
                    fl_estab  = st.text_input("Estabelecimento contém", key="ll_estab", placeholder="Digite parte do nome...")
                with fl2:
                    fl_tipo   = st.selectbox("Tipo de pagamento", ["Todos"] + sorted(df_all["tipo_pagamento"].dropna().unique().tolist()) if "tipo_pagamento" in df_all.columns else ["Todos"], key="ll_tipo")
                    fl_comp   = st.selectbox("Comprado por", ["Todos"] + sorted([x for x in df_all["comprado_por"].dropna().unique().tolist() if x != ""]) if "comprado_por" in df_all.columns else ["Todos"], key="ll_comp")
                    fl_comp_para = st.selectbox("Comprado para", ["Todos"] + sorted([x for x in df_all["comprado_para"].dropna().unique().tolist() if x != ""]) if "comprado_para" in df_all.columns else ["Todos"], key="ll_comp_para")
                with fl3:
                    fl_cat    = st.selectbox("Categoria", ["Todas"] + sorted(df_all["categoria"].dropna().unique().tolist()) if "categoria" in df_all.columns else ["Todas"], key="ll_cat")
                    fl_per    = st.selectbox("Periodicidade", ["Todas"] + [p for p in PERIODICIDADES if p], key="ll_per")
                with fl4:
                    if fl_cat != "Todas" and "subcategoria" in df_all.columns:
                        subs_ll = ["Todas"] + sorted(df_all[df_all["categoria"]==fl_cat]["subcategoria"].dropna().unique().tolist())
                    else:
                        subs_ll = ["Todas"] + sorted(df_all["subcategoria"].dropna().unique().tolist()) if "subcategoria" in df_all.columns else ["Todas"]
                    fl_sub = st.selectbox("Subcategoria", subs_ll, key="ll_sub")
                    fl_imp = st.selectbox("Importância", ["Todas"] + [i for i in IMPORTANCIAS if i], key="ll_imp")

            # Aplica filtros
            df_f = df_all.copy()
            if fl_mes    != "Todos":  df_f = df_f[df_f["mes_referencia"] == fl_mes]
            if fl_cartao != "Todos":  df_f = df_f[df_f["cartao"] == fl_cartao]
            if fl_estab.strip():      df_f = df_f[df_f["estabelecimento"].str.contains(fl_estab.strip(), case=False, na=False)]
            if fl_tipo   != "Todos":  df_f = df_f[df_f["tipo_pagamento"] == fl_tipo]
            if fl_comp   != "Todos":  df_f = df_f[df_f["comprado_por"] == fl_comp]
            if fl_comp_para != "Todos": df_f = df_f[df_f["comprado_para"] == fl_comp_para]
            if fl_cat    != "Todas":  df_f = df_f[df_f["categoria"] == fl_cat]
            if fl_sub    != "Todas":  df_f = df_f[df_f["subcategoria"] == fl_sub]
            if fl_per    != "Todas" and "periodicidade" in df_f.columns:  df_f = df_f[df_f["periodicidade"] == fl_per]
            if fl_imp    != "Todas" and "importancia" in df_f.columns:    df_f = df_f[df_f["importancia"] == fl_imp]

            # Exibe resultado
            if "tipo_pagamento" in df_f.columns:
                df_f_show = df_f.copy()
                df_f_show["tipo_pagamento"] = df_f_show["tipo_pagamento"].apply(tipo_label)
            else:
                df_f_show = df_f
            
            st.dataframe(df_f_show, use_container_width=True, hide_index=True)
            total_f = df_f["valor_parcela"].astype(float).sum() if "valor_parcela" in df_f.columns else 0.0
            ci2, ct2 = st.columns([3, 1])
            with ci2: st.caption(f"{len(df_f)} de {len(df_all)} lançamento(s)")
            with ct2: st.metric("Valor total", formatar_brl(total_f))

            if len(df_f) == 0:
                st.info("Nenhum lançamento encontrado com os filtros selecionados.")
            else:
                ids_lanc = df_f["id"].tolist()
                # Verifica se há cartão de crédito no resultado
                tem_cartao_cc = "tipo_pagamento" in df_f.columns and (df_f["tipo_pagamento"] == COD_CARTAO).any()

                st.divider()
                
                # --- Seleção com checkboxes ---
                st.markdown("### ✅ Selecione os lançamentos para operação")
                
                if "lote_selecionados" not in st.session_state:
                    st.session_state.lote_selecionados = set()
                
                col_sel, col_desc, col_est, col_comp, col_val = st.columns([0.5, 1.5, 2.5, 1.5, 1])
                col_sel.markdown("**Sel.**")
                col_desc.markdown("**Data**")
                col_est.markdown("**Estabelecimento**")
                col_comp.markdown("**Comprado Por**")
                col_val.markdown("**Valor**")
                
                for idx, row in df_f.iterrows():
                    row_id = row["id"]
                    c_sel, c_desc, c_est, c_comp, c_val = st.columns([0.5, 1.5, 2.5, 1.5, 1])
                    
                    with c_sel:
                        if st.checkbox("", value=row_id in st.session_state.lote_selecionados, key=f"chk_{row_id}"):
                            st.session_state.lote_selecionados.add(row_id)
                        else:
                            st.session_state.lote_selecionados.discard(row_id)
                    
                    with c_desc:
                        st.text(str(row.get("data_origem", "")))
                    with c_est:
                        st.text(str(row.get("estabelecimento", ""))[:40])
                    with c_comp:
                        st.text(str(row.get("comprado_por", "")))
                    with c_val:
                        st.text(f"R$ {float(row.get('valor_parcela', 0)):.2f}")
                
                ids_selecionados = list(st.session_state.lote_selecionados)
                
                if not ids_selecionados:
                    st.info("Selecione ao menos um lançamento para executar uma operação.")
                else:
                    st.divider()
                    
                    # Filtra dados selecionados
                    df_sel = df_f[df_f["id"].isin(ids_selecionados)].copy()
                    tem_cartao_selecionado = "tipo_pagamento" in df_sel.columns and (df_sel["tipo_pagamento"] == COD_CARTAO).any()
                    
                    op_lanc = st.radio(
                        "Operação sobre os lançamentos selecionados",
                        ["✏️ Alterar campos selecionados", "🗑️ Excluir lançamentos selecionados"],
                        horizontal=True, key="op_lote_lanc"
                    )

                    if op_lanc == "✏️ Alterar campos selecionados":
                        st.caption("⚠️ Para lançamentos de cartão de crédito, apenas o campo 'Comprado para' pode ser alterado. Para outros lançamentos, todos os campos disponíveis podem ser alterados.")
                        
                        with st.form("form_lote_lanc_alt"):
                            ll1, ll2 = st.columns(2)
                            
                            # Se houver cartão de crédito selecionado, mostrar apenas comprado_para
                            if tem_cartao_selecionado and not "tipo_pagamento" in df_sel.columns or (df_sel["tipo_pagamento"] == COD_CARTAO).all():
                                with ll1:
                                    st.info("🔒 Apenas lançamentos de cartão selecionados. Apenas 'Comprado para' pode ser alterado.")
                                    ll_comp_para_alt = st.text_input("Comprado para (em branco = manter)")
                                ok_lote_lanc = st.form_submit_button(f"💾 Alterar {len(ids_selecionados)} lançamento(s)")
                                
                                if ok_lote_lanc:
                                    try:
                                        payload_lanc = {}
                                        if ll_comp_para_alt.strip():
                                            payload_lanc["comprado_para"] = ll_comp_para_alt.strip()
                                        
                                        if not payload_lanc:
                                            st.warning("Nenhum campo preenchido para alterar.")
                                        else:
                                            for id_ in ids_selecionados:
                                                supabase.table("lancamentos").update(payload_lanc).eq("id", id_).execute()
                                            st.success(f"{len(ids_selecionados)} lançamento(s) atualizado(s) com sucesso!")
                                            st.session_state.lote_selecionados = set()
                                            st.rerun()
                                    except Exception as e:
                                        st.error(f"Erro: {e}")
                            else:
                                with ll1:
                                    ll_estab  = st.text_input("Estabelecimento (em branco = manter)")
                                    ll_desc   = st.text_input("Descrição (em branco = manter)")
                                    ll_comp   = st.text_input("Comprado por (em branco = manter)")
                                    ll_comp_para_alt = st.text_input("Comprado para (em branco = manter)")
                                    ll_cat    = st.selectbox("Categoria", ["(não alterar)"] + cats_disp)
                                with ll2:
                                    ll_sub_opts = ["(não alterar)"] + sorted(subcats_map.get(ll_cat, {"Outros"})) if ll_cat != "(não alterar)" else ["(não alterar)"]
                                    ll_sub    = st.selectbox("Subcategoria", ll_sub_opts)
                                    ll_tipo   = st.selectbox("Tipo de pagamento", ["(não alterar)"] + TIPOS_NAO_CARTAO)
                                    ll_per    = st.selectbox("Periodicidade", ["(não alterar)"] + [p for p in PERIODICIDADES if p])
                                    ll_imp    = st.selectbox("Importância", ["(não alterar)"] + [i for i in IMPORTANCIAS if i])
                                ok_lote_lanc = st.form_submit_button(f"💾 Alterar {len(ids_selecionados)} lançamento(s)")

                                if ok_lote_lanc:
                                    try:
                                        payload_lanc = {}
                                        if ll_estab.strip():          payload_lanc["estabelecimento"] = ll_estab.strip()
                                        if ll_desc.strip():           payload_lanc["descricao"]        = ll_desc.strip()
                                        if ll_comp.strip():           payload_lanc["comprado_por"]     = normalizar_comprador(ll_comp.strip())
                                        if ll_comp_para_alt.strip():  payload_lanc["comprado_para"]   = ll_comp_para_alt.strip()
                                        if ll_cat  != "(não alterar)": payload_lanc["categoria"]       = ll_cat
                                        if ll_sub  != "(não alterar)": payload_lanc["subcategoria"]    = ll_sub
                                        if ll_tipo != "(não alterar)": payload_lanc["tipo_pagamento"]  = ll_tipo[:2]
                                        if ll_per  != "(não alterar)": payload_lanc["periodicidade"]   = ll_per
                                        if ll_imp  != "(não alterar)": payload_lanc["importancia"]     = ll_imp

                                        if not payload_lanc:
                                            st.warning("Nenhum campo preenchido para alterar.")
                                        else:
                                            # Se há cartão selecionado junto com outros, avisar que apenas comprado_para será alterado nos cartões
                                            if tem_cartao_selecionado:
                                                st.warning("⚠️ Para lançamentos de cartão de crédito, apenas 'Comprado para' será alterado. Outros campos serão ignorados.")
                                                for id_ in ids_selecionados:
                                                    df_item = df_f[df_f["id"] == id_]
                                                    if "tipo_pagamento" in df_item.columns and (df_item["tipo_pagamento"] == COD_CARTAO).values[0]:
                                                        # Apenas comprado_para para cartão
                                                        payload_cartao = {k: v for k, v in payload_lanc.items() if k == "comprado_para"}
                                                        if payload_cartao:
                                                            supabase.table("lancamentos").update(payload_cartao).eq("id", id_).execute()
                                                    else:
                                                        # Todos os campos para não-cartão
                                                        supabase.table("lancamentos").update(payload_lanc).eq("id", id_).execute()
                                            else:
                                                for id_ in ids_selecionados:
                                                    supabase.table("lancamentos").update(payload_lanc).eq("id", id_).execute()
                                            
                                            st.success(f"{len(ids_selecionados)} lançamento(s) atualizado(s) com sucesso!")
                                            st.session_state.lote_selecionados = set()
                                            st.rerun()
                                    except Exception as e:
                                        st.error(f"Erro: {e}")

                    else:  # Excluir
                        if tem_cartao_selecionado:
                            st.error("⛔ Os lançamentos selecionados contêm cartão de crédito que não podem ser excluídos aqui. Refine a seleção para excluir apenas lançamentos de outras fontes.")
                        else:
                            st.warning(f"⚠️ Isso irá excluir **{len(ids_selecionados)} lançamento(s)** permanentemente. Esta ação não pode ser desfeita.")
                            if st.button(f"🗑️ Confirmar exclusão de {len(ids_selecionados)} lançamento(s)", key="btn_excl_lote"):
                                try:
                                    for id_ in ids_selecionados:
                                        supabase.table("lancamentos").delete().eq("id", id_).execute()
                                    st.success(f"{len(ids_selecionados)} lançamento(s) excluído(s)!")
                                    st.session_state.lote_selecionados = set()
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Erro: {e}")

    # ABA SEM CLASSIFICAÇÃO
    with aba[4]:
        st.subheader("Lançamentos sem classificação")
        st.caption(
            "Selecione o mês, o comprador e a Categoria/Subcategoria a aplicar. "
            "O sistema pré-preenche a tabela — edite individualmente se necessário e salve."
        )

        cats_padrao = _config.get("categorias_padrao", [])

        if not cats_padrao:
            st.warning(
                "Nenhuma **Categoria Padrão por Comprador** configurada. "
                "Acesse ⚙️ Configurações → 🏷️ Categoria Padrão por Comprador para cadastrar."
            )
        else:
            # --- Linha 1: mês + comprador ---
            sc1, sc2 = st.columns(2)
            with sc1:
                _meses_sc = _gerar_meses_opcoes()
                sc_mes = st.selectbox(
                    "Mês de referência",
                    _meses_sc,
                    format_func=formatar_mes_mmaaaa,
                    key="sc_mes",
                )
            with sc2:
                _sc_compradores = sorted(set(cp["comprado_para"] for cp in cats_padrao))
                sc_comprador = st.selectbox("Comprado para", _sc_compradores, key="sc_comp_para")

            # Padrão configurado para o comprador selecionado
            sc_cfg = next((cp for cp in cats_padrao if cp["comprado_para"] == sc_comprador), {})
            sc_cat_def    = sc_cfg.get("categoria", "")
            sc_subcat_def = sc_cfg.get("subcategoria", "")

            # --- Linha 2: categoria + subcategoria restritas às configurações ---
            # Categorias disponíveis = somente as configuradas em categorias_padrao para este comprador
            sc_cats_cfg = sorted(set(
                cp["categoria"] for cp in cats_padrao if cp["comprado_para"] == sc_comprador
            ))
            sc3, sc4 = st.columns(2)
            with sc3:
                sc_cat_idx = sc_cats_cfg.index(sc_cat_def) if sc_cat_def in sc_cats_cfg else 0
                sc_cat = st.selectbox(
                    "Categoria",
                    sc_cats_cfg,
                    index=sc_cat_idx,
                    key=f"sc_cat_{sc_comprador}",
                )
            with sc4:
                # Subcategorias disponíveis = somente as configuradas em categorias_padrao
                # para este comprador + categoria selecionada
                sc_subcats_cfg = sorted(set(
                    cp["subcategoria"] for cp in cats_padrao
                    if cp["comprado_para"] == sc_comprador and cp["categoria"] == sc_cat
                ))
                sc_subcat_idx = sc_subcats_cfg.index(sc_subcat_def) if sc_subcat_def in sc_subcats_cfg else 0
                sc_subcat = st.selectbox(
                    "Subcategoria",
                    sc_subcats_cfg,
                    index=sc_subcat_idx,
                    key=f"sc_subcat_{sc_comprador}_{sc_cat}",
                )

            # --- Busca e filtra ---
            # Considera "sem classificação" lançamentos que:
            # 1. Estão sem categoria (vazio ou "Outros"), OU
            # 2. Têm a categoria/subcategoria padrão configurada para este comprador
            #    (foram auto-classificados pelo fallback e ainda precisam de classificação real)
            todos_sc = buscar_lancamentos_db()
            sem_class = [
                l for l in todos_sc
                if l.get("mes_referencia") == sc_mes
                and l.get("comprado_para") == sc_comprador
                and (
                    str(l.get("categoria", "")).strip() in ("", "Outros")
                    or (
                        l.get("categoria")   == sc_cat_def
                        and l.get("subcategoria") == sc_subcat_def
                    )
                )
            ]

            if not sem_class:
                st.success(f"✅ Nenhum lançamento sem classificação para **{sc_comprador}** em {formatar_mes_mmaaaa(sc_mes)}.")
            else:
                st.info(
                    f"{len(sem_class)} lançamento(s) sem classificação para **{sc_comprador}**. "
                    f"Serão classificados como **{sc_cat} / {sc_subcat}**."
                )

                # Monta DataFrame — pré-preenche com categoria/subcategoria selecionadas
                cols_sc = ["id", "estabelecimento", "comprado_para", "categoria", "subcategoria", "periodicidade", "importancia"]
                df_sc_orig = pd.DataFrame(sem_class)[[c for c in cols_sc if c in sem_class[0]]]

                for col, default in [("periodicidade", ""), ("importancia", "")]:
                    if col not in df_sc_orig.columns:
                        df_sc_orig[col] = default
                    else:
                        df_sc_orig[col] = df_sc_orig[col].fillna("").replace("", default)

                df_sc_orig["categoria"]    = sc_cat
                df_sc_orig["subcategoria"] = sc_subcat
                df_sc_orig = df_sc_orig.reset_index(drop=True)

                with st.expander("➕ Cadastrar nova Categoria / Subcategoria"):
                    with st.form("form_sc_nova_cat"):
                        ncc1, ncc2 = st.columns(2)
                        with ncc1:
                            sc_nova_cat = st.text_input("Categoria", key="sc_nova_cat")
                        with ncc2:
                            sc_nova_sub = st.text_input("Subcategoria", key="sc_nova_sub")
                        ok_sc_cat = st.form_submit_button("✅ Incluir categoria")
                    if ok_sc_cat:
                        if not sc_nova_cat.strip():
                            st.warning("Categoria é obrigatória.")
                        else:
                            try:
                                supabase.table("categorias").insert({
                                    "categoria":    sc_nova_cat.strip(),
                                    "subcategoria": sc_nova_sub.strip(),
                                    "familia_id":   FAMILIA_ID,
                                }).execute()
                                st.success(f'Categoria "{sc_nova_cat.strip()} / {sc_nova_sub.strip()}" incluída!')
                                st.rerun()
                            except Exception as e:
                                st.error(f"Erro: {e}")

                opts_per_sc = [p for p in PERIODICIDADES if p]
                opts_imp_sc = [i for i in IMPORTANCIAS if i]

                # Dropdowns do editor usam a lista completa de categorias e subcategorias cadastradas
                todas_subcats_editor = sorted(set(s for subs in subcats_map.values() for s in subs) | {"Outros"})

                edited_sc = st.data_editor(
                    df_sc_orig,
                    column_config={
                        "id":              st.column_config.Column(label="ID",              disabled=True, width="small"),
                        "estabelecimento": st.column_config.TextColumn("Estabelecimento",   disabled=True, width="large"),
                        "comprado_para":   st.column_config.SelectboxColumn("Comprado Para", options=_compradores_para, width="medium"),
                        "categoria":       st.column_config.SelectboxColumn("Categoria",    options=cats_disp,            width="medium"),
                        "subcategoria":    st.column_config.SelectboxColumn("Subcategoria", options=todas_subcats_editor, width="medium"),
                        "periodicidade":   st.column_config.SelectboxColumn("Periodicidade", options=opts_per_sc,         width="small"),
                        "importancia":     st.column_config.SelectboxColumn("Importância",  options=opts_imp_sc,          width="small"),
                    },
                    use_container_width=True,
                    hide_index=True,
                    key=f"sc_editor_{sc_mes}_{sc_comprador}_{sc_cat}_{sc_subcat}",
                )

                if st.button("💾 Salvar alterações", key="btn_sc_salvar"):
                    try:
                        alterados = 0
                        for i in range(len(edited_sc)):
                            row_ed = edited_sc.iloc[i]
                            payload_sc = {
                                "categoria":     row_ed["categoria"],
                                "subcategoria":  row_ed["subcategoria"],
                                "comprado_para": row_ed["comprado_para"],
                            }
                            if row_ed.get("periodicidade"):
                                payload_sc["periodicidade"] = row_ed["periodicidade"]
                            if row_ed.get("importancia"):
                                payload_sc["importancia"] = row_ed["importancia"]
                            supabase.table("lancamentos").update(payload_sc).eq("id", str(row_ed["id"])).execute()
                            alterados += 1
                        if alterados:
                            st.success(f"{alterados} lançamento(s) classificado(s) com sucesso!")
                            st.rerun()
                    except Exception as e:
                        st.error(f"Erro: {e}")

    # ----------------------------------------------------------------
    # ABA LISTAR / ALTERAR / EXCLUIR
    # ----------------------------------------------------------------
    with aba[5]:
        st.subheader("Listar / Alterar / Excluir lançamentos")
        st.caption("Filtre, edite diretamente na tabela e salve. A exclusão é feita na seção abaixo.")

        # --- Filtros ---
        lae1, lae2 = st.columns(2)
        with lae1:
            _meses_lae = _gerar_meses_opcoes()
            lae_mes = st.selectbox(
                "Mês de referência",
                ["Todos"] + _meses_lae,
                format_func=lambda x: x if x == "Todos" else formatar_mes_mmaaaa(x),
                key="lae_mes",
            )
        with lae2:
            lae_comp_para = st.selectbox(
                "Comprado para",
                ["Todos"] + _compradores_para,
                key="lae_comp_para",
            )

        lae3, lae4 = st.columns(2)
        with lae3:
            lae_cat = st.selectbox(
                "Categoria",
                ["Todas"] + cats_disp,
                key="lae_cat",
            )
        with lae4:
            if lae_cat != "Todas":
                lae_subcats = ["Todas"] + sorted(subcats_map.get(lae_cat, set()))
            else:
                lae_subcats = ["Todas"] + sorted(set(s for subs in subcats_map.values() for s in subs))
            lae_sub = st.selectbox(
                "Subcategoria",
                lae_subcats,
                key=f"lae_sub_{lae_cat}",
            )

        # --- Busca e filtra ---
        todos_lae = buscar_lancamentos_db()
        filtrado_lae = todos_lae
        if lae_mes      != "Todos":  filtrado_lae = [l for l in filtrado_lae if l.get("mes_referencia") == lae_mes]
        if lae_comp_para != "Todos": filtrado_lae = [l for l in filtrado_lae if l.get("comprado_para")  == lae_comp_para]
        if lae_cat      != "Todas":  filtrado_lae = [l for l in filtrado_lae if l.get("categoria")      == lae_cat]
        if lae_sub      != "Todas":  filtrado_lae = [l for l in filtrado_lae if l.get("subcategoria")   == lae_sub]

        if not filtrado_lae:
            st.info("Nenhum lançamento encontrado para os filtros selecionados.")
        else:
            st.caption(f"{len(filtrado_lae)} lançamento(s) encontrado(s).")

            # Expander para nova categoria
            with st.expander("➕ Cadastrar nova Categoria / Subcategoria"):
                with st.form("form_lae_nova_cat"):
                    lnc1, lnc2 = st.columns(2)
                    with lnc1:
                        lae_nova_cat = st.text_input("Categoria", key="lae_nova_cat")
                    with lnc2:
                        lae_nova_sub = st.text_input("Subcategoria", key="lae_nova_sub")
                    ok_lae_cat = st.form_submit_button("✅ Incluir categoria")
                if ok_lae_cat:
                    if not lae_nova_cat.strip():
                        st.warning("Categoria é obrigatória.")
                    else:
                        try:
                            supabase.table("categorias").insert({
                                "categoria":    lae_nova_cat.strip(),
                                "subcategoria": lae_nova_sub.strip(),
                                "familia_id":   FAMILIA_ID,
                            }).execute()
                            st.success(f'Categoria "{lae_nova_cat.strip()} / {lae_nova_sub.strip()}" incluída!')
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro: {e}")

            # Monta DataFrame
            cols_lae = ["id", "data_origem", "estabelecimento", "valor_parcela", "cartao",
                        "comprado_para", "categoria", "subcategoria", "periodicidade", "importancia"]
            df_lae_orig = pd.DataFrame(filtrado_lae)[[c for c in cols_lae if c in filtrado_lae[0]]]

            for col, default in [("categoria", "Outros"), ("subcategoria", "Outros"),
                                  ("periodicidade", ""), ("importancia", ""), ("comprado_para", "")]:
                if col not in df_lae_orig.columns:
                    df_lae_orig[col] = default
                else:
                    df_lae_orig[col] = df_lae_orig[col].fillna(default)

            df_lae_orig = df_lae_orig.reset_index(drop=True)

            todas_subcats_lae = sorted(set(s for subs in subcats_map.values() for s in subs) | {"Outros"})
            opts_per_lae = [p for p in PERIODICIDADES if p]
            opts_imp_lae = [i for i in IMPORTANCIAS if i]

            edited_lae = st.data_editor(
                df_lae_orig,
                column_config={
                    "id":            st.column_config.Column(label="ID",            disabled=True, width="small"),
                    "data_origem":   st.column_config.TextColumn("Data",            disabled=True, width="small"),
                    "estabelecimento": st.column_config.TextColumn("Estabelecimento", disabled=True, width="large"),
                    "valor_parcela": st.column_config.NumberColumn("Valor (R$)",    disabled=True, format="%.2f", width="small"),
                    "cartao":        st.column_config.TextColumn("Cartão",          disabled=True, width="medium"),
                    "comprado_para": st.column_config.SelectboxColumn("Comprado Para", options=_compradores_para, width="medium"),
                    "categoria":     st.column_config.SelectboxColumn("Categoria",    options=cats_disp,         width="medium"),
                    "subcategoria":  st.column_config.SelectboxColumn("Subcategoria", options=todas_subcats_lae, width="medium"),
                    "periodicidade": st.column_config.SelectboxColumn("Periodicidade", options=opts_per_lae,     width="small"),
                    "importancia":   st.column_config.SelectboxColumn("Importância",  options=opts_imp_lae,      width="small"),
                },
                use_container_width=True,
                hide_index=True,
                key=f"lae_editor_{lae_mes}_{lae_comp_para}_{lae_cat}_{lae_sub}",
            )

            if st.button("💾 Salvar alterações", key="btn_lae_salvar"):
                try:
                    alterados_lae = 0
                    for i in range(len(edited_lae)):
                        row_ed  = edited_lae.iloc[i]
                        row_ori = df_lae_orig.iloc[i]
                        payload_lae = {}
                        if str(row_ed["comprado_para"]) != str(row_ori["comprado_para"]): payload_lae["comprado_para"] = row_ed["comprado_para"]
                        if str(row_ed["categoria"])     != str(row_ori["categoria"]):     payload_lae["categoria"]     = row_ed["categoria"]
                        if str(row_ed["subcategoria"])  != str(row_ori["subcategoria"]):  payload_lae["subcategoria"]  = row_ed["subcategoria"]
                        if str(row_ed.get("periodicidade","")) != str(row_ori.get("periodicidade","")): payload_lae["periodicidade"] = row_ed["periodicidade"] or None
                        if str(row_ed.get("importancia",""))   != str(row_ori.get("importancia","")):   payload_lae["importancia"]   = row_ed["importancia"] or None
                        if payload_lae:
                            supabase.table("lancamentos").update(payload_lae).eq("id", str(row_ed["id"])).execute()
                            alterados_lae += 1
                    if alterados_lae:
                        st.success(f"{alterados_lae} lançamento(s) atualizado(s)!")
                        st.rerun()
                    else:
                        st.info("Nenhuma alteração detectada.")
                except Exception as e:
                    st.error(f"Erro: {e}")

            # --- Seção de exclusão ---
            st.divider()
            st.subheader("🗑️ Excluir lançamentos")
            _lae_opts_del = {
                f"{l.get('data_origem','')} | {str(l.get('estabelecimento',''))[:35]} | R$ {float(l.get('valor_parcela',0)):.2f} | {l.get('comprado_para','')}": l["id"]
                for l in filtrado_lae
            }
            lae_del_sel = st.multiselect(
                "Selecione os lançamentos a excluir",
                list(_lae_opts_del.keys()),
                key="lae_del_sel",
            )
            if lae_del_sel:
                st.warning(f"⚠️ {len(lae_del_sel)} lançamento(s) serão excluídos permanentemente.")
                if st.button(f"🗑️ Confirmar exclusão de {len(lae_del_sel)} lançamento(s)", key="btn_lae_del"):
                    try:
                        for label in lae_del_sel:
                            supabase.table("lancamentos").delete().eq("id", _lae_opts_del[label]).execute()
                        st.success(f"{len(lae_del_sel)} lançamento(s) excluído(s)!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Erro: {e}")


# =============================================================
# PÁGINA 3 — RELATÓRIOS
# =============================================================
elif pagina == "📊 Relatórios":

    st.header("📊 Relatórios")

    lancamentos_todos = buscar_lancamentos_db()
    meses_disponiveis = []
    if lancamentos_todos and "mes_referencia" in lancamentos_todos[0]:
        meses_disponiveis = sorted(
            pd.DataFrame(lancamentos_todos)["mes_referencia"].dropna().unique().tolist(),
            reverse=True,
        )

    tipo_rel = st.selectbox(
        "Escolha o relatório",
        ["📋 Lançamentos (tabela)", "🌳 Por Categoria",
         "📅 Por Categoria + Compras Futuras", "📊 Comparativo por Mês"],
    )

    # ===========================================================
    # RELATÓRIO 3 — COMPARATIVO POR MÊS
    # ===========================================================
    if tipo_rel == "📊 Comparativo por Mês":
        st.subheader("📊 Comparativo por Mês")

        if not lancamentos_todos:
            st.info("Nenhum lançamento cadastrado.")
            st.stop()

        meses_comp = st.multiselect(
            "Meses para comparar",
            options=meses_disponiveis,
            default=meses_disponiveis[:3] if len(meses_disponiveis) >= 3 else meses_disponiveis,
            format_func=formatar_mes_mmaaaa,
            key="rel_comp_meses",
        )

        if not meses_comp:
            st.info("Selecione ao menos um mês para visualizar o comparativo.")
            st.stop()

        df_comp = pd.DataFrame(lancamentos_todos)
        df_comp = df_comp[df_comp["mes_referencia"].isin(meses_comp)].copy()
        df_comp["valor_parcela"] = df_comp["valor_parcela"].astype(float)

        # Filtro opcional de categoria
        with st.expander("🔍 Filtros adicionais", expanded=False):
            xc1, xc2 = st.columns(2)
            with xc1:
                cats_comp_disp = ["Todas"] + sorted(df_comp["categoria"].dropna().unique().tolist())
                xf_cat = st.selectbox("Categoria", cats_comp_disp, key="comp_cat")
            with xc2:
                if xf_cat != "Todas":
                    subs_comp_disp = ["Todas"] + sorted(df_comp[df_comp["categoria"] == xf_cat]["subcategoria"].dropna().unique().tolist())
                else:
                    subs_comp_disp = ["Todas"] + sorted(df_comp["subcategoria"].dropna().unique().tolist())
                xf_sub = st.selectbox("Subcategoria", subs_comp_disp, key="comp_sub")
        if xf_cat != "Todas":
            df_comp = df_comp[df_comp["categoria"] == xf_cat]
        if xf_sub != "Todas":
            df_comp = df_comp[df_comp["subcategoria"] == xf_sub]

        if df_comp.empty:
            st.info("Nenhum lançamento encontrado para os filtros selecionados.")
            st.stop()

        # Pivot: linhas = (categoria, subcategoria), colunas = mês
        meses_ord = sorted(meses_comp)
        pivot = (
            df_comp.groupby(["categoria", "subcategoria", "mes_referencia"])["valor_parcela"]
            .sum()
            .unstack(level="mes_referencia", fill_value=0)
            .reindex(columns=meses_ord, fill_value=0)
        )

        # Coluna extra: Diferença (só com 2 meses) ou Total (3+ meses)
        dois_meses = len(meses_ord) == 2
        if dois_meses:
            pivot["Diferença"] = pivot[meses_ord[1]] - pivot[meses_ord[0]]
            col_extra = "Diferença"
        else:
            pivot["Total"] = pivot[meses_ord].sum(axis=1)
            col_extra = "Total"

        cat_totals  = pivot.groupby(level="categoria").sum()
        grand_total = pivot.sum()

        def _fmt_diff_str(v: float) -> str:
            return (f"+{formatar_brl(v)}" if v > 0 else formatar_brl(v))

        def _html_extra(v: float) -> str:
            if dois_meses:
                txt   = _fmt_diff_str(v)
                color = "#e53935" if v > 0 else ("#2e7d32" if v < 0 else "inherit")
                return f"<div style='text-align:right;color:{color}'><b>{txt}</b></div>"
            return f"<div style='text-align:right'>{formatar_brl(v)}</div>"

        # ------- Tabela-resumo por categoria no topo --------
        resumo_rows = []
        for cat in sorted(cat_totals.index):
            row = {"Categoria": cat}
            for m in meses_ord:
                row[formatar_mes(m)] = formatar_brl(cat_totals.at[cat, m])
            v_extra = cat_totals.at[cat, col_extra]
            row[col_extra] = _fmt_diff_str(v_extra) if dois_meses else formatar_brl(v_extra)
            resumo_rows.append(row)
        total_row = {"Categoria": "TOTAL GERAL"}
        for m in meses_ord:
            total_row[formatar_mes(m)] = formatar_brl(grand_total[m])
        v_extra_gt = grand_total[col_extra]
        total_row[col_extra] = _fmt_diff_str(v_extra_gt) if dois_meses else formatar_brl(v_extra_gt)
        resumo_rows.append(total_row)

        st.dataframe(pd.DataFrame(resumo_rows), use_container_width=True, hide_index=True)
        st.divider()

        # ------- Árvore detalhada por categoria / subcategoria -------
        if "subcat_aberta_comp" not in st.session_state:
            st.session_state.subcat_aberta_comp = None

        n_meses    = len(meses_ord)
        col_widths = [4] + [2] * n_meses + [2, 1]

        for cat in sorted(cat_totals.index):
            t_cat = cat_totals.loc[cat]
            meses_header = "   |   ".join(
                f"{formatar_mes(m)}: {formatar_brl(t_cat[m])}" for m in meses_ord
            )
            extra_header = (
                f"   |   {col_extra}: {_fmt_diff_str(t_cat[col_extra])}"
                if dois_meses else
                f"   |   Total: {formatar_brl(t_cat[col_extra])}"
            )
            with st.expander(
                f"📁 {cat}   —   {meses_header}{extra_header}",
                expanded=True,
            ):
                # Cabeçalho de colunas
                hdr_cols = st.columns(col_widths)
                hdr_cols[0].markdown("**Subcategoria**")
                for i, m in enumerate(meses_ord):
                    hdr_cols[i + 1].markdown(
                        f"<div style='text-align:right'><b>{formatar_mes(m)}</b></div>",
                        unsafe_allow_html=True,
                    )
                hdr_cols[n_meses + 1].markdown(
                    f"<div style='text-align:right'><b>{col_extra}</b></div>",
                    unsafe_allow_html=True,
                )

                df_cat_pivot = pivot.loc[cat].copy()
                for subcat, row in df_cat_pivot.iterrows():
                    chave_comp = f"comp_{cat}||{subcat}"
                    aberta     = st.session_state.subcat_aberta_comp == chave_comp
                    row_cols   = st.columns(col_widths)
                    row_cols[0].markdown(f"&nbsp;&nbsp;📄 **{subcat}**", unsafe_allow_html=True)
                    for i, m in enumerate(meses_ord):
                        row_cols[i + 1].markdown(
                            f"<div style='text-align:right'>{formatar_brl(row[m])}</div>",
                            unsafe_allow_html=True,
                        )
                    row_cols[n_meses + 1].markdown(
                        _html_extra(row[col_extra]),
                        unsafe_allow_html=True,
                    )
                    if row_cols[n_meses + 2].button("▲" if aberta else "▼", key=f"btn_{chave_comp}"):
                        st.session_state.subcat_aberta_comp = None if aberta else chave_comp
                        st.rerun()

                    if aberta:
                        df_det = df_comp[
                            (df_comp["categoria"] == cat) &
                            (df_comp["subcategoria"] == subcat)
                        ].copy()
                        cols_det = [c for c in [
                            "mes_referencia", "data_origem", "estabelecimento",
                            "descricao", "comprado_por", "comprado_para", "valor_parcela",
                        ] if c in df_det.columns]
                        df_det_show = df_det[cols_det].sort_values(["mes_referencia", "data_origem"])
                        if "mes_referencia" in df_det_show.columns:
                            df_det_show = df_det_show.copy()
                            df_det_show["mes_referencia"] = df_det_show["mes_referencia"].apply(formatar_mes)
                        st.dataframe(df_det_show, use_container_width=True, hide_index=True)

        # Rodapé com totais gerais
        st.divider()
        footer_cols = st.columns(col_widths[:-1])
        footer_cols[0].markdown("**TOTAL GERAL**")
        for i, m in enumerate(meses_ord):
            footer_cols[i + 1].markdown(
                f"<div style='text-align:right'><b>{formatar_brl(grand_total[m])}</b></div>",
                unsafe_allow_html=True,
            )
        footer_cols[n_meses + 1].markdown(
            _html_extra(grand_total[col_extra]).replace(
                "style='text-align:right", "style='text-align:right;font-weight:bold"
            ),
            unsafe_allow_html=True,
        )

    else:
        # ===========================================================
        # RELATÓRIOS 1, 2 e 3 — requerem mês único
        # ===========================================================
        mes_selecionado = st.selectbox(
            "Mês de referência",
            ["— Selecione um mês —"] + meses_disponiveis,
            key="rel_mes_ref",
            format_func=lambda x: x if x == "— Selecione um mês —" else formatar_mes_mmaaaa(x),
        )

        if mes_selecionado == "— Selecione um mês —":
            st.info("Selecione o mês de referência para visualizar os relatórios.")
            st.stop()

        if not lancamentos_todos:
            st.info("Nenhum lançamento cadastrado.")
        else:
            cols_base = ["id","data_origem","estabelecimento","descricao","comprado_por","comprado_para",
                         "valor_parcela","cartao","mes_referencia","tipo_pagamento","categoria","subcategoria",
                         "tipo_categoria","periodicidade","importancia"]
            cols_ex   = [c for c in cols_base if c in lancamentos_todos[0]]
            df_base   = pd.DataFrame(lancamentos_todos)[cols_ex]
            if "tipo_pagamento" in df_base.columns:
                df_base["tipo_pagamento"] = df_base["tipo_pagamento"].apply(tipo_label)

            # Pré-filtra pelo mês selecionado
            if "mes_referencia" in df_base.columns:
                df_base = df_base[df_base["mes_referencia"] == mes_selecionado]

            with st.expander("🔍 Filtros adicionais", expanded=False):
                fc1, fc2 = st.columns(2)
                with fc1:
                    cartoes_disp = ["Todos"] + sorted(df_base["cartao"].dropna().unique().tolist()) if "cartao" in df_base.columns else ["Todos"]
                    f_cartao = st.selectbox("Cartão / Banco", cartoes_disp)
                with fc2:
                    tipos_disp = ["Todos"] + sorted(df_base["tipo_pagamento"].dropna().unique().tolist()) if "tipo_pagamento" in df_base.columns else ["Todos"]
                    f_tipo = st.selectbox("Tipo de pagamento", tipos_disp)

                fc4, fc5, fc6 = st.columns(3)
                with fc4:
                    cats_disp = ["Todas"] + sorted(df_base["categoria"].dropna().unique().tolist()) if "categoria" in df_base.columns else ["Todas"]
                    f_cat = st.selectbox("Categoria", cats_disp)
                with fc5:
                    if f_cat != "Todas" and "subcategoria" in df_base.columns:
                        subs_disp = ["Todas"] + sorted(df_base[df_base["categoria"] == f_cat]["subcategoria"].dropna().unique().tolist())
                    else:
                        subs_disp = ["Todas"] + sorted(df_base["subcategoria"].dropna().unique().tolist()) if "subcategoria" in df_base.columns else ["Todas"]
                    f_sub = st.selectbox("Subcategoria", subs_disp)
                with fc6:
                    compradores_disp = ["Todos"] + sorted([x for x in df_base["comprado_por"].dropna().unique().tolist() if x != ""]) if "comprado_por" in df_base.columns else ["Todos"]
                    f_comprador = st.selectbox("Comprado por", compradores_disp)

                fc7, fc8, fc9, fc10 = st.columns(4)
                with fc7:
                    tipo_cat_disp = ["Todos"] + [t for t in TIPOS_CATEGORIA if t]
                    f_tipo_cat = st.selectbox("Tipo de categoria", tipo_cat_disp)
                with fc8:
                    f_period = st.selectbox("Periodicidade", ["Todas"] + [p for p in PERIODICIDADES if p])
                with fc9:
                    f_import = st.selectbox("Importância", ["Todas"] + [i for i in IMPORTANCIAS if i])
                with fc10:
                    comp_para_rel_disp = ["Todos"] + sorted([x for x in df_base["comprado_para"].dropna().unique().tolist() if x != ""]) if "comprado_para" in df_base.columns else ["Todos"]
                    f_comp_para = st.selectbox("Comprado para", comp_para_rel_disp, key="rel_comp_para")

            df_rel = df_base.copy()
            if f_cartao     != "Todos":  df_rel = df_rel[df_rel["cartao"] == f_cartao]
            if f_tipo       != "Todos":  df_rel = df_rel[df_rel["tipo_pagamento"] == f_tipo]
            if f_cat        != "Todas":  df_rel = df_rel[df_rel["categoria"] == f_cat]
            if f_sub        != "Todas":  df_rel = df_rel[df_rel["subcategoria"] == f_sub]
            if f_comprador  != "Todos":  df_rel = df_rel[df_rel["comprado_por"] == f_comprador]
            if f_comp_para  != "Todos" and "comprado_para" in df_rel.columns:
                df_rel = df_rel[df_rel["comprado_para"] == f_comp_para]
            if f_tipo_cat   != "Todos"  and "tipo_categoria" in df_rel.columns:
                df_rel = df_rel[df_rel["tipo_categoria"] == f_tipo_cat]
            if f_period     != "Todas"  and "periodicidade" in df_rel.columns:
                df_rel = df_rel[df_rel["periodicidade"] == f_period]
            if f_import     != "Todas"  and "importancia" in df_rel.columns:
                df_rel = df_rel[df_rel["importancia"] == f_import]

            # -------------------------------------------------------
            # RELATÓRIO 1 — TABELA SIMPLES
            # -------------------------------------------------------
            if tipo_rel == "📋 Lançamentos (tabela)":
                st.subheader("📋 Lançamentos")
                st.dataframe(df_rel, use_container_width=True, hide_index=True)
                total = df_rel["valor_parcela"].astype(float).sum() if "valor_parcela" in df_rel.columns else 0.0
                ci, ct = st.columns([3, 1])
                with ci: st.caption(f"{len(df_rel)} lançamento(s)")
                with ct: st.metric("Total", formatar_brl(total))

            # -------------------------------------------------------
            # RELATÓRIO 2 — POR CATEGORIA (árvore)
            # -------------------------------------------------------
            elif tipo_rel == "🌳 Por Categoria":
                st.subheader("🌳 Relatório por Categoria")

                if df_rel.empty:
                    st.info("Nenhum lançamento encontrado para os filtros selecionados.")
                else:
                    df_rel["valor_parcela"] = df_rel["valor_parcela"].astype(float)

                    total_geral = df_rel["valor_parcela"].sum()
                    por_cat     = df_rel.groupby("categoria")["valor_parcela"].sum().sort_index()
                    por_subcat  = df_rel.groupby(["categoria","subcategoria"])["valor_parcela"].sum()

                    if "subcat_aberta" not in st.session_state:
                        st.session_state.subcat_aberta = None

                    h1, h2, h3 = st.columns([6, 2, 2])
                    with h1: st.markdown("**Categoria / Subcategoria**")
                    with h2: st.markdown("<div style='text-align:right'><b>Total</b></div>", unsafe_allow_html=True)
                    with h3: st.markdown("<div style='text-align:right'><b>%</b></div>", unsafe_allow_html=True)
                    st.divider()

                    for cat, total_cat in por_cat.items():
                        pct_cat = (total_cat / total_geral * 100) if total_geral else 0
                        with st.expander(f"📁 {cat}   —   {formatar_brl(total_cat)}   ({pct_cat:.1f}%)", expanded=True):
                            if cat in por_subcat:
                                subcats = por_subcat[cat].sort_index()
                                for subcat, total_sub in subcats.items():
                                    pct_sub = (total_sub / total_geral * 100) if total_geral else 0
                                    chave   = f"{cat}||{subcat}"
                                    sc1, sc2, sc3, sc4 = st.columns([5, 2, 2, 1])
                                    with sc1:
                                        st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;📄 **{subcat}**", unsafe_allow_html=True)
                                    with sc2:
                                        st.markdown(f"<div style='text-align:right'>{formatar_brl(total_sub)}</div>", unsafe_allow_html=True)
                                    with sc3:
                                        st.markdown(f"<div style='text-align:right'>{pct_sub:.1f}%</div>", unsafe_allow_html=True)
                                    with sc4:
                                        aberta = st.session_state.subcat_aberta == chave
                                        if st.button("▲" if aberta else "▼", key=f"btn_{chave}"):
                                            st.session_state.subcat_aberta = None if aberta else chave
                                            st.rerun()
                                    if st.session_state.subcat_aberta == chave:
                                        df_sub = df_rel[
                                            (df_rel["categoria"] == cat) &
                                            (df_rel["subcategoria"] == subcat)
                                        ].copy()
                                        cols_show = [c for c in ["data_origem","estabelecimento","descricao",
                                                                  "comprado_por","comprado_para","cartao","valor_parcela",
                                                                  "tipo_pagamento"] if c in df_sub.columns]
                                        st.dataframe(
                                            df_sub[cols_show].sort_values("data_origem"),
                                            use_container_width=True, hide_index=True,
                                        )

                    st.divider()
                    ct1, ct2, ct3 = st.columns([6, 2, 2])
                    with ct1: st.markdown("**TOTAL GERAL**")
                    with ct2: st.markdown(f"<div style='text-align:right'><b>{formatar_brl(total_geral)}</b></div>", unsafe_allow_html=True)
                    with ct3: st.markdown("<div style='text-align:right'><b>100%</b></div>", unsafe_allow_html=True)

            # -------------------------------------------------------
            # RELATÓRIO 3 — POR CATEGORIA + COMPRAS FUTURAS (tabela)
            # -------------------------------------------------------
            elif tipo_rel == "📅 Por Categoria + Compras Futuras":
                st.subheader("📅 Por Categoria + Compras Futuras")

                if df_rel.empty:
                    st.info("Nenhum lançamento encontrado para os filtros selecionados.")
                else:
                    df_rel["valor_parcela"] = df_rel["valor_parcela"].astype(float)

                    # Lê o padrão "N/M" e projeta parcelas restantes nos meses seguintes
                    def _parse_parcelas(estab: str, desc: str = "") -> tuple:
                        for a_s, t_s in re.findall(r'(?<!\d)(\d{1,2})/(\d{1,2})(?!\d|/)', f"{estab} {desc}"):
                            a, t = int(a_s), int(t_s)
                            if 1 <= a < t <= 48:
                                return a, t
                        return None, None

                    def _add_months(mes_iso: str, n: int) -> str:
                        y, m, _ = mes_iso.split("-")
                        y, m = int(y), int(m) + n
                        y += (m - 1) // 12
                        m = ((m - 1) % 12) + 1
                        return f"{y:04d}-{m:02d}-01"

                    future_data: dict = {}
                    df_cc = df_rel[df_rel["tipo_pagamento"].astype(str).str.startswith(COD_CARTAO)]
                    for _, row in df_cc.iterrows():
                        a, t = _parse_parcelas(
                            str(row.get("estabelecimento", "")),
                            str(row.get("descricao", "")),
                        )
                        if a is None:
                            continue
                        key = (row.get("categoria", "Outros"), row.get("subcategoria", "Outros"))
                        val = float(row.get("valor_parcela", 0.0))
                        for offset in range(1, t - a + 1):
                            bucket = future_data.setdefault(offset, {})
                            bucket[key] = bucket.get(key, 0.0) + val

                    fut_lookup: dict = {
                        _add_months(mes_selecionado, off): data
                        for off, data in sorted(future_data.items())
                    }
                    meses_futuros = sorted(fut_lookup.keys())

                    def _vf(cat, subcat, mes):
                        return float(fut_lookup.get(mes, {}).get((cat, subcat), 0.0))

                    def _vf_cat(cat, mes):
                        return sum(v for (c, _), v in fut_lookup.get(mes, {}).items() if c == cat)

                    total_geral = df_rel["valor_parcela"].sum()
                    por_cat     = df_rel.groupby("categoria")["valor_parcela"].sum().sort_index()
                    por_subcat  = df_rel.groupby(["categoria","subcategoria"])["valor_parcela"].sum()

                    col_vigente = formatar_mes(mes_selecionado)
                    colunas_mes = [col_vigente] + [formatar_mes(mf) for mf in meses_futuros]

                    ROW_CAT   = "cat"
                    ROW_SUB   = "sub"
                    ROW_TOTAL = "total"
                    linhas = []

                    for cat in sorted(por_cat.index):
                        total_cat = por_cat[cat]
                        cat_row = {"Categoria / Subcategoria": f"📁  {cat}"}
                        cat_row[col_vigente] = formatar_brl(total_cat)
                        for mf, lbl in zip(meses_futuros, colunas_mes[1:]):
                            v = _vf_cat(cat, mf)
                            cat_row[lbl] = formatar_brl(v) if v else "—"
                        linhas.append((ROW_CAT, cat_row))

                        if cat in por_subcat:
                            for subcat, total_sub in por_subcat[cat].sort_index().items():
                                sub_row = {"Categoria / Subcategoria": f"    {subcat}"}
                                sub_row[col_vigente] = formatar_brl(total_sub)
                                for mf, lbl in zip(meses_futuros, colunas_mes[1:]):
                                    v = _vf(cat, subcat, mf)
                                    sub_row[lbl] = formatar_brl(v) if v else "—"
                                linhas.append((ROW_SUB, sub_row))

                    total_row = {"Categoria / Subcategoria": "TOTAL GERAL"}
                    total_row[col_vigente] = formatar_brl(total_geral)
                    for mf, lbl in zip(meses_futuros, colunas_mes[1:]):
                        v_tot = sum(fut_lookup.get(mf, {}).values())
                        total_row[lbl] = formatar_brl(v_tot) if v_tot else "—"
                    linhas.append((ROW_TOTAL, total_row))

                    df_table = pd.DataFrame([d for _, d in linhas])
                    tipos    = [t for t, _ in linhas]

                    def _estilo_linha(row):
                        t = tipos[row.name]
                        if t == ROW_CAT:
                            return ["font-weight:600; background-color:#f7f7f7"] * len(row)
                        if t == ROW_TOTAL:
                            return ["font-weight:700; background-color:#ececec"] * len(row)
                        return ["color:#444444"] * len(row)

                    st.dataframe(
                        df_table.style.apply(_estilo_linha, axis=1),
                        use_container_width=True,
                        hide_index=True,
                    )


# =============================================================
# PÁGINA 4 — CATEGORIAS
# =============================================================
elif pagina == "📂 Categorias":

    st.header("📂 Categorias")
    st.caption("Clique em ✏️ para editar ou 🗑️ para excluir. Use os botões ➕ para adicionar.")

    # --- session state ---
    for _k, _v in [
        ("cat_editing_id",      None),
        ("cat_renaming",        None),
        ("cat_adding_sub",      None),
        ("cat_adding_new",      False),
        ("cat_del_confirm",     None),
        ("cat_del_cat_confirm", None),
    ]:
        if _k not in st.session_state:
            st.session_state[_k] = _v

    cats_db = buscar_categorias_db()

    from collections import defaultdict as _dd
    _grupos: dict = _dd(list)
    for _item in cats_db:
        _grupos[_item["categoria"]].append(_item)

    # ── Cabeçalho + botão nova categoria ──
    _h1, _h2 = st.columns([5, 2])
    with _h1:
        st.subheader(f"{len(cats_db)} item(s) em {len(_grupos)} categoria(s)")
    with _h2:
        if st.button("➕ Nova categoria", use_container_width=True, key="btn_new_cat_top"):
            st.session_state.cat_adding_new    = True
            st.session_state.cat_renaming      = None
            st.session_state.cat_editing_id    = None
            st.session_state.cat_adding_sub    = None
            st.rerun()

    # Formulário de nova categoria (topo)
    if st.session_state.cat_adding_new:
        with st.form("form_cat_new_top"):
            _nc1, _nc2 = st.columns(2)
            with _nc1:
                _new_cat = st.text_input("Nome da categoria", placeholder="Ex: Alimentação")
            with _nc2:
                _new_sub = st.text_input("Primeira subcategoria", placeholder="Ex: Mercado")
            _btn1, _btn2 = st.columns(2)
            with _btn1:
                _ok_new = st.form_submit_button("✅ Criar", use_container_width=True)
            with _btn2:
                _cancel_new = st.form_submit_button("❌ Cancelar", use_container_width=True)
        if _ok_new:
            if not _new_cat.strip():
                st.warning("Informe o nome da categoria.")
            else:
                try:
                    supabase.table("categorias").insert({
                        "categoria":    _new_cat.strip(),
                        "subcategoria": _new_sub.strip(),
                        "familia_id":   FAMILIA_ID,
                    }).execute()
                    st.session_state.cat_adding_new = False
                    st.success(f'Categoria "{_new_cat.strip()}" criada.')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")
        if _cancel_new:
            st.session_state.cat_adding_new = False
            st.rerun()

    if not cats_db:
        st.info("Nenhuma categoria cadastrada. Clique em '➕ Nova categoria' para começar.")

    else:
        st.divider()

        for _cat_name in sorted(_grupos.keys()):
            _items = sorted(_grupos[_cat_name], key=lambda x: x.get("subcategoria", ""))
            _cat_slug = _cat_name.replace(" ", "_").replace("/", "_")

            # ── Cabeçalho da categoria ──
            if st.session_state.cat_renaming == _cat_name:
                with st.form(f"form_rename_{_cat_slug}"):
                    _rn1, _rn2, _rn3 = st.columns([5, 1, 1])
                    with _rn1:
                        _rn_val = st.text_input("", value=_cat_name, label_visibility="collapsed")
                    with _rn2:
                        _ok_rn = st.form_submit_button("💾", use_container_width=True)
                    with _rn3:
                        _cancel_rn = st.form_submit_button("❌", use_container_width=True)
                if _ok_rn:
                    if _rn_val.strip() and _rn_val.strip() != _cat_name:
                        try:
                            for _it in _items:
                                supabase.table("categorias").update(
                                    {"categoria": _rn_val.strip()}
                                ).eq("id", _it["id"]).execute()
                            st.session_state.cat_renaming = None
                            st.success(f'Categoria renomeada para "{_rn_val.strip()}".')
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro: {e}")
                    else:
                        st.session_state.cat_renaming = None
                        st.rerun()
                if _cancel_rn:
                    st.session_state.cat_renaming = None
                    st.rerun()
            else:
                _ch1, _ch2, _ch3 = st.columns([5, 2, 2])
                with _ch1:
                    st.markdown(
                        f"**📂 {_cat_name}**"
                        f"<span style='color:#888;font-size:0.85em'>"
                        f"&nbsp;({len(_items)} subcategoria{'s' if len(_items) != 1 else ''})"
                        f"</span>",
                        unsafe_allow_html=True,
                    )
                with _ch2:
                    if st.button("✏️ Renomear", key=f"btn_rn_{_cat_slug}", use_container_width=True):
                        st.session_state.cat_renaming      = _cat_name
                        st.session_state.cat_editing_id    = None
                        st.session_state.cat_adding_sub    = None
                        st.session_state.cat_del_cat_confirm = None
                        st.rerun()
                with _ch3:
                    if st.session_state.cat_del_cat_confirm == _cat_name:
                        if st.button(
                            "⚠️ Confirmar", key=f"btn_delcat_conf_{_cat_slug}",
                            type="primary", use_container_width=True,
                        ):
                            try:
                                for _it in _items:
                                    supabase.table("categorias").delete().eq("id", _it["id"]).execute()
                                st.session_state.cat_del_cat_confirm = None
                                st.success(f'"{_cat_name}" excluída.')
                                st.rerun()
                            except Exception as e:
                                st.error(f"Erro: {e}")
                    else:
                        if st.button("🗑️ Excluir", key=f"btn_delcat_{_cat_slug}", use_container_width=True):
                            st.session_state.cat_del_cat_confirm = _cat_name
                            st.session_state.cat_editing_id      = None
                            st.session_state.cat_renaming        = None
                            st.rerun()

            # ── Subcategorias ──
            for _item in _items:
                _iid = _item["id"]
                _is_editing     = st.session_state.cat_editing_id == _iid
                _is_del_confirm = st.session_state.cat_del_confirm == _iid

                if _is_editing:
                    with st.form(f"form_edit_sub_{_iid}"):
                        _ec1, _ec2, _ec3 = st.columns([5, 1, 1])
                        with _ec1:
                            _edit_val = st.text_input(
                                "", value=_item["subcategoria"],
                                label_visibility="collapsed",
                            )
                        with _ec2:
                            _ok_edit = st.form_submit_button("💾", use_container_width=True)
                        with _ec3:
                            _cancel_edit = st.form_submit_button("❌", use_container_width=True)
                    if _ok_edit:
                        try:
                            supabase.table("categorias").update(
                                {"subcategoria": _edit_val.strip()}
                            ).eq("id", _iid).execute()
                            st.session_state.cat_editing_id = None
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro: {e}")
                    if _cancel_edit:
                        st.session_state.cat_editing_id = None
                        st.rerun()

                elif _is_del_confirm:
                    _dc1, _dc2, _dc3 = st.columns([5, 2, 1])
                    with _dc1:
                        st.markdown(
                            f"&nbsp;&nbsp;&nbsp;&nbsp;↳ "
                            f"<span style='color:#e05;text-decoration:line-through'>"
                            f"{_item['subcategoria']}</span>",
                            unsafe_allow_html=True,
                        )
                    with _dc2:
                        if st.button("⚠️ Confirmar", key=f"conf_del_{_iid}",
                                     type="primary", use_container_width=True):
                            try:
                                supabase.table("categorias").delete().eq("id", _iid).execute()
                                st.session_state.cat_del_confirm = None
                                st.rerun()
                            except Exception as e:
                                st.error(f"Erro: {e}")
                    with _dc3:
                        if st.button("❌", key=f"cancel_del_{_iid}", use_container_width=True):
                            st.session_state.cat_del_confirm = None
                            st.rerun()

                else:
                    _sc1, _sc2, _sc3 = st.columns([5, 1, 1])
                    with _sc1:
                        st.markdown(f"&nbsp;&nbsp;&nbsp;&nbsp;↳ {_item['subcategoria']}")
                    with _sc2:
                        if st.button("✏️", key=f"edit_{_iid}",
                                     use_container_width=True, help="Editar subcategoria"):
                            st.session_state.cat_editing_id      = _iid
                            st.session_state.cat_renaming        = None
                            st.session_state.cat_del_confirm     = None
                            st.session_state.cat_del_cat_confirm = None
                            st.rerun()
                    with _sc3:
                        if st.button("🗑️", key=f"del_{_iid}",
                                     use_container_width=True, help="Excluir subcategoria"):
                            st.session_state.cat_del_confirm     = _iid
                            st.session_state.cat_editing_id      = None
                            st.session_state.cat_del_cat_confirm = None
                            st.rerun()

            # ── Adicionar subcategoria ──
            if st.session_state.cat_adding_sub == _cat_name:
                with st.form(f"form_add_sub_{_cat_slug}"):
                    _as1, _as2, _as3 = st.columns([5, 1, 1])
                    with _as1:
                        _new_sub_val = st.text_input(
                            "", placeholder="Nova subcategoria",
                            label_visibility="collapsed",
                        )
                    with _as2:
                        _ok_sub = st.form_submit_button("✅", use_container_width=True)
                    with _as3:
                        _cancel_sub = st.form_submit_button("❌", use_container_width=True)
                if _ok_sub:
                    if not _new_sub_val.strip():
                        st.warning("Informe o nome da subcategoria.")
                    else:
                        try:
                            supabase.table("categorias").insert({
                                "categoria":    _cat_name,
                                "subcategoria": _new_sub_val.strip(),
                                "familia_id":   FAMILIA_ID,
                            }).execute()
                            st.session_state.cat_adding_sub = None
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro: {e}")
                if _cancel_sub:
                    st.session_state.cat_adding_sub = None
                    st.rerun()
            else:
                _add1, _add2, _add3 = st.columns([5, 2, 1])
                with _add2:
                    if st.button("➕ Subcategoria", key=f"btn_add_sub_{_cat_slug}",
                                 use_container_width=True):
                        st.session_state.cat_adding_sub      = _cat_name
                        st.session_state.cat_editing_id      = None
                        st.session_state.cat_renaming        = None
                        st.session_state.cat_del_cat_confirm = None
                        st.rerun()

            st.divider()


# =============================================================
# PÁGINA 5 — REGRAS DE CLASSIFICAÇÃO
# =============================================================
elif pagina == "🏷️ Regras de Classificação":

    st.header("🏷️ Regras de Classificação")
    aba = st.tabs(["➕ Incluir", "🔍 Consultar / Alterar / Excluir"])

    # ---- ABA INCLUIR ----
    with aba[0]:
        st.subheader("Nova regra")
        with st.form("form_cat_inc"):
            palavra     = st.text_input("Palavra-chave")
            cat         = st.text_input("Categoria")
            subcat      = st.text_input("Subcategoria")
            desc_c      = st.text_input("Descrição", help="Se preenchida, será propagada para o lançamento ao classificar.")
            estab_c     = st.text_input("Estabelecimento", help="Se preenchido, substitui o nome do estabelecimento no lançamento ao classificar.")
            tipo_c      = st.selectbox("Tipo de pagamento", ["(não propagar)"] + TIPOS_PAGAMENTO_LISTA, help="Se selecionado, substitui o tipo de pagamento no lançamento ao classificar.")
            tipo_cat_c  = st.selectbox("Tipo de categoria", TIPOS_CATEGORIA, help="Classifica o tipo de despesa desta categoria.")
            per_c       = st.selectbox("Periodicidade", PERIODICIDADES, help="Fixa Mensal ou Eventual.")
            imp_c       = st.selectbox("Importância", IMPORTANCIAS, help="Essencial ou não essencial.")
            comp_para_c = st.selectbox(
                "Comprado para",
                ["(não propagar)"] + _compradores_para,
                help="Se selecionado, define automaticamente para quem é a compra ao classificar.",
            )
            ok          = st.form_submit_button("✅ Incluir")
        if ok:
            if not palavra or not cat:
                st.warning("Palavra-chave e Categoria são obrigatórios.")
            else:
                try:
                    cod_tipo = tipo_c[:2] if tipo_c != "(não propagar)" else ""
                    supabase.table("regras_classificacao").insert({
                        "palavra_chave":   palavra.strip(),
                        "categoria":       cat.strip(),
                        "subcategoria":    subcat.strip(),
                        "descricao":       desc_c.strip(),
                        "estabelecimento": estab_c.strip(),
                        "tipo_pagamento":  cod_tipo,
                        "tipo_categoria":  tipo_cat_c.strip(),
                        "periodicidade":   per_c.strip(),
                        "importancia":     imp_c.strip(),
                        "comprado_para":   comp_para_c if comp_para_c != "(não propagar)" else "",
                        "familia_id":      FAMILIA_ID,
                    }).execute()
                    st.success(f'Regra "{palavra}" incluída!')
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ---- ABA CONSULTAR / ALTERAR / EXCLUIR ----
    with aba[1]:
        st.subheader("Consultar, alterar ou excluir regras")

        # --- Filtros de consulta ---
        with st.expander("🔍 Filtros de consulta", expanded=True):
            fc1, fc2, fc3, fc4_reg = st.columns(4)
            with fc1:
                f_pk   = st.text_input("Palavra-chave contém", key="fcat_pk")
                f_cat  = st.selectbox("Categoria", ["Todas"] + sorted(set(r["categoria"] for r in buscar_regras_db())), key="fcat_cat")
            with fc2:
                f_sub  = st.text_input("Subcategoria contém", key="fcat_sub")
                f_per  = st.selectbox("Periodicidade", ["Todas"] + [p for p in PERIODICIDADES if p], key="fcat_per")
            with fc3:
                f_imp  = st.selectbox("Importância", ["Todas"] + [i for i in IMPORTANCIAS if i], key="fcat_imp")
                f_tc   = st.selectbox("Tipo de categoria", ["Todos"] + [t for t in TIPOS_CATEGORIA if t], key="fcat_tc")
            with fc4_reg:
                f_cp_regra = st.selectbox(
                    "Comprado para",
                    ["Todos"] + _compradores_para + ["(vazio)"],
                    key="fcat_cp",
                )

        # Aplica filtros
        todas_regras = buscar_regras_db()
        regras_filtradas = todas_regras
        if f_pk:
            regras_filtradas = [r for r in regras_filtradas if f_pk.upper() in r["palavra_chave"].upper()]
        if f_cat != "Todas":
            regras_filtradas = [r for r in regras_filtradas if r["categoria"] == f_cat]
        if f_sub:
            regras_filtradas = [r for r in regras_filtradas if f_sub.upper() in (r.get("subcategoria") or "").upper()]
        if f_per != "Todas":
            regras_filtradas = [r for r in regras_filtradas if r.get("periodicidade") == f_per]
        if f_imp != "Todas":
            regras_filtradas = [r for r in regras_filtradas if r.get("importancia") == f_imp]
        if f_tc != "Todos":
            regras_filtradas = [r for r in regras_filtradas if r.get("tipo_categoria") == f_tc]
        if f_cp_regra != "Todos":
            if f_cp_regra == "(vazio)":
                regras_filtradas = [r for r in regras_filtradas if not r.get("comprado_para")]
            else:
                regras_filtradas = [r for r in regras_filtradas if r.get("comprado_para") == f_cp_regra]

        if not regras_filtradas:
            st.info("Nenhuma regra encontrada com os filtros selecionados.")
        else:
            # Exibe resultado da consulta
            cols_c  = ["id","palavra_chave","categoria","subcategoria","descricao","estabelecimento",
                       "tipo_pagamento","tipo_categoria","periodicidade","importancia","comprado_para"]
            cols_ex = [c for c in cols_c if c in regras_filtradas[0]]
            df_r    = pd.DataFrame(regras_filtradas)[cols_ex]
            nomes   = ["ID","Palavra-chave","Categoria","Subcategoria","Descrição","Estabelecimento",
                       "Tipo Pagamento","Tipo Categoria","Periodicidade","Importância","Comprado Para"]
            df_r.columns = nomes[:len(cols_ex)]
            if "Tipo Pagamento" in df_r.columns:
                df_r["Tipo Pagamento"] = df_r["Tipo Pagamento"].apply(lambda x: tipo_label(x) if x else "")
            st.dataframe(df_r, use_container_width=True, hide_index=True)
            st.caption(f"{len(regras_filtradas)} regra(s) encontrada(s) de {len(todas_regras)} no total")

            st.divider()
            st.markdown("### ✅ Selecione as regras para operação")

            if "regras_selecionadas" not in st.session_state:
                st.session_state.regras_selecionadas = set()

            rh1, rh2, rh3, rh4, rh5 = st.columns([0.5, 2.5, 3, 1.5, 1.5])
            rh1.markdown("**Sel.**")
            rh2.markdown("**Palavra-chave**")
            rh3.markdown("**Categoria / Subcategoria**")
            rh4.markdown("**Comprado Para**")
            rh5.markdown("**Periodicidade**")

            for r in regras_filtradas:
                rid = r["id"]
                rc1, rc2, rc3, rc4, rc5 = st.columns([0.5, 2.5, 3, 1.5, 1.5])
                with rc1:
                    checked = st.checkbox("", value=rid in st.session_state.regras_selecionadas, key=f"rchk_{rid}")
                    if checked:
                        st.session_state.regras_selecionadas.add(rid)
                    else:
                        st.session_state.regras_selecionadas.discard(rid)
                with rc2:
                    st.text(str(r.get("palavra_chave", ""))[:35])
                with rc3:
                    st.text(f"{r.get('categoria','')}/{r.get('subcategoria','')}"[:45])
                with rc4:
                    st.text(str(r.get("comprado_para", "") or ""))
                with rc5:
                    st.text(str(r.get("periodicidade", "") or ""))

            ids_filtrados = list(st.session_state.regras_selecionadas & {r["id"] for r in regras_filtradas})

            if not ids_filtrados:
                st.info("Selecione ao menos uma regra para executar uma operação.")
            else:
                st.divider()

                # --- Operação sobre selecionadas ---
                op = st.radio(
                    f"Operação sobre as {len(ids_filtrados)} regra(s) selecionada(s)",
                    ["✏️ Alterar campos selecionados", "🗑️ Excluir regras selecionadas"],
                    horizontal=True,
                )

                if op == "✏️ Alterar campos selecionados":
                    st.caption("Preencha apenas os campos que deseja alterar. Campos em branco ou com '(não alterar)' serão mantidos.")
                    with st.form("form_cat_lote_alt"):
                        la1, la2 = st.columns(2)
                        with la1:
                            lote_cat    = st.text_input("Categoria (em branco = manter)")
                            lote_sub    = st.text_input("Subcategoria (em branco = manter)")
                            lote_desc   = st.text_input("Descrição (em branco = manter)")
                            lote_estab  = st.text_input("Estabelecimento (em branco = manter)")
                            lote_cp     = st.selectbox(
                                "Comprado para",
                                ["(não alterar)"] + _compradores_para + ["(limpar)"],
                            )
                        with la2:
                            lote_tp     = st.selectbox("Tipo de pagamento", ["(não alterar)"] + TIPOS_PAGAMENTO_LISTA)
                            lote_tc     = st.selectbox("Tipo de categoria", ["(não alterar)"] + [t for t in TIPOS_CATEGORIA if t])
                            lote_per    = st.selectbox("Periodicidade", ["(não alterar)"] + [p for p in PERIODICIDADES if p])
                            lote_imp    = st.selectbox("Importância", ["(não alterar)"] + [i for i in IMPORTANCIAS if i])
                        ok_lote = st.form_submit_button(f"💾 Alterar {len(ids_filtrados)} regra(s)")

                    if ok_lote:
                        try:
                            payload = {}
                            if lote_cat.strip():   payload["categoria"]       = lote_cat.strip()
                            if lote_sub.strip():   payload["subcategoria"]    = lote_sub.strip()
                            if lote_desc.strip():  payload["descricao"]       = lote_desc.strip()
                            if lote_estab.strip(): payload["estabelecimento"] = lote_estab.strip()
                            if lote_tp  != "(não alterar)": payload["tipo_pagamento"] = lote_tp[:2]
                            if lote_tc  != "(não alterar)": payload["tipo_categoria"] = lote_tc
                            if lote_per != "(não alterar)": payload["periodicidade"]  = lote_per
                            if lote_imp != "(não alterar)": payload["importancia"]    = lote_imp
                            if lote_cp  != "(não alterar)":
                                payload["comprado_para"] = "" if lote_cp == "(limpar)" else lote_cp

                            if not payload:
                                st.warning("Nenhum campo preenchido para alterar.")
                            else:
                                for id_ in ids_filtrados:
                                    supabase.table("regras_classificacao").update(payload).eq("id", id_).execute()
                                st.success(f"{len(ids_filtrados)} regra(s) atualizada(s) com sucesso!")
                                st.session_state.regras_selecionadas = set()
                                st.rerun()
                        except Exception as e:
                            st.error(f"Erro: {e}")

                else:
                    st.warning(f"⚠️ Isso irá excluir **{len(ids_filtrados)} regra(s)** permanentemente. Esta ação não pode ser desfeita.")
                    if st.button(f"🗑️ Confirmar exclusão de {len(ids_filtrados)} regra(s)"):
                        try:
                            for id_ in ids_filtrados:
                                supabase.table("regras_classificacao").delete().eq("id", id_).execute()
                            st.success(f"{len(ids_filtrados)} regra(s) excluída(s) com sucesso!")
                            st.session_state.regras_selecionadas = set()
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro: {e}")

# =============================================================
# PÁGINA 6 — CONFIGURAÇÕES
# =============================================================
elif pagina == "⚙️ Configurações":
    st.header("⚙️ Configurações da Família")

    abas_cfg = st.tabs([
        "👥 Membros",
        "🏦 Instituições",
        "🔁 Lançamentos Fixos",
        "🗂️ Mapeamento de Compradores",
        "🚫 Estabelecimentos Ignorados",
        "🏷️ Categoria Padrão por Comprador",
        "👤 Usuários Autorizados",
    ])

    # ----------------------------------------------------------------
    # ABA 1 — MEMBROS
    # ----------------------------------------------------------------
    with abas_cfg[0]:
        st.subheader("Membros da família")

        membros_db = sorted(_config["membros"], key=lambda m: m.get("nome", ""))

        if membros_db:
            df_mem = pd.DataFrame(membros_db)[["nome"]]
            df_mem.columns = ["Nome"]
            st.dataframe(df_mem, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum membro cadastrado.")

        st.divider()
        st.subheader("Incluir novo membro")
        with st.form("form_membro_add"):
            novo_nome = st.text_input("Nome do membro")
            ok_mem = st.form_submit_button("➕ Incluir")
        if ok_mem:
            if not novo_nome.strip():
                st.warning("Informe o nome do membro.")
            else:
                try:
                    supabase.rpc("insert_membro", {"p_familia_id": FAMILIA_ID, "p_nome": novo_nome.strip()}).execute()
                    st.cache_data.clear()
                    st.success(f'Membro "{novo_nome.strip()}" incluído!')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

        if membros_db:
            st.divider()
            st.subheader("Excluir membro")
            mem_opts = {m["nome"]: m["id"] for m in membros_db}
            mem_del = st.selectbox("Selecione o membro a excluir", list(mem_opts.keys()), key="mem_del_sel")
            st.warning("⚠️ Excluir um membro pode afetar mapeamentos e lançamentos fixos associados.")
            if st.button("🗑️ Excluir membro selecionado", key="btn_mem_del"):
                try:
                    supabase.rpc("delete_membro", {"p_id": mem_opts[mem_del], "p_familia_id": FAMILIA_ID}).execute()
                    st.cache_data.clear()
                    st.success(f'Membro "{mem_del}" excluído.')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ----------------------------------------------------------------
    # ABA 2 — INSTITUIÇÕES
    # ----------------------------------------------------------------
    with abas_cfg[1]:
        st.subheader("Instituições (cartões e bancos)")

        inst_db = sorted(_config["instituicoes"], key=lambda i: i.get("nome", ""))

        if inst_db:
            cols_inst = ["nome", "chave_checklist", "tipo", "padroes_arquivo", "comprador_fixo", "comprador_padrao", "tem_multiplos_portadores"]
            df_inst = pd.DataFrame(inst_db)[[c for c in cols_inst if c in inst_db[0]]]
            df_inst.columns = ["Nome", "Chave Checklist", "Tipo", "Padrões Arquivo", "Comprador Fixo", "Comprador Padrão", "Múlt. Portadores"]
            df_inst["Padrões Arquivo"] = df_inst["Padrões Arquivo"].apply(lambda x: ", ".join(x) if isinstance(x, list) else x)
            st.dataframe(df_inst, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhuma instituição cadastrada.")

        st.divider()
        st.subheader("Incluir nova instituição")
        with st.form("form_inst_add"):
            ci1, ci2 = st.columns(2)
            with ci1:
                inst_nome    = st.text_input("Nome da instituição", help="Ex: C6 Black")
                inst_chave   = st.text_input("Chave checklist", help="Ex: C6")
                inst_tipo    = st.selectbox("Tipo", ["fatura", "extrato"])
                inst_padroes = st.text_input("Padrões de arquivo (separados por vírgula)", help="Ex: C6,C6 BLACK")
            with ci2:
                inst_comp_fixo = st.selectbox("Comprador fixo", ["(nenhum)"] + _compradores_para)
                inst_comp_pad  = st.selectbox("Comprador padrão", ["(nenhum)"] + _compradores_para)
                inst_multiplos = st.checkbox("Tem múltiplos portadores")
            ok_inst = st.form_submit_button("➕ Incluir")
        if ok_inst:
            if not inst_nome.strip() or not inst_chave.strip() or not inst_padroes.strip():
                st.warning("Nome, chave checklist e padrões de arquivo são obrigatórios.")
            else:
                try:
                    padroes_lista = [p.strip() for p in inst_padroes.split(",") if p.strip()]
                    supabase.rpc("insert_instituicao_familia", {
                        "p_familia_id":               FAMILIA_ID,
                        "p_nome":                     inst_nome.strip(),
                        "p_chave_checklist":          inst_chave.strip(),
                        "p_tipo":                     inst_tipo,
                        "p_padroes_arquivo":          padroes_lista,
                        "p_comprador_fixo":           None if inst_comp_fixo == "(nenhum)" else inst_comp_fixo,
                        "p_comprador_padrao":         None if inst_comp_pad  == "(nenhum)" else inst_comp_pad,
                        "p_tem_multiplos_portadores": inst_multiplos,
                    }).execute()
                    st.cache_data.clear()
                    st.success(f'Instituição "{inst_nome.strip()}" incluída!')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

        if inst_db:
            st.divider()
            st.subheader("Excluir instituição")
            inst_opts = {i["nome"]: i["id"] for i in inst_db}
            inst_del  = st.selectbox("Selecione a instituição a excluir", list(inst_opts.keys()), key="inst_del_sel")
            st.warning("⚠️ Excluir uma instituição remove também seus mapeamentos de compradores associados.")
            if st.button("🗑️ Excluir instituição selecionada", key="btn_inst_del"):
                try:
                    supabase.rpc("delete_instituicao_familia", {"p_id": inst_opts[inst_del], "p_familia_id": FAMILIA_ID}).execute()
                    st.cache_data.clear()
                    st.success(f'Instituição "{inst_del}" excluída.')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ----------------------------------------------------------------
    # ABA 3 — LANÇAMENTOS FIXOS
    # ----------------------------------------------------------------
    with abas_cfg[2]:
        st.subheader("Lançamentos fixos mensais")

        fixos_db = sorted(_config["fixos"], key=lambda f: f.get("estabelecimento", ""))

        if fixos_db:
            cols_fix = ["estabelecimento", "descricao", "valor_parcela", "categoria", "subcategoria",
                        "comprado_por", "comprado_para", "tipo_pagamento", "periodicidade", "importancia",
                        "cartao", "gatilho"]
            df_fix = pd.DataFrame(fixos_db)[[c for c in cols_fix if c in fixos_db[0]]]
            df_fix.columns = ["Estabelecimento", "Descrição", "Valor (R$)", "Categoria", "Subcategoria",
                               "Comprado Por", "Comprado Para", "Tipo Pgto", "Periodicidade", "Importância",
                               "Cartão", "Gatilho"]
            df_fix["Valor (R$)"] = df_fix["Valor (R$)"].apply(lambda v: f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
            df_fix["Tipo Pgto"] = df_fix["Tipo Pgto"].apply(lambda x: TIPOS_PAGAMENTO.get(str(x), str(x) if x else ""))
            st.dataframe(df_fix, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum lançamento fixo cadastrado.")

        st.divider()
        st.subheader("Incluir novo lançamento fixo")
        _nomes_cartoes_fixo = [i["nome"] for i in _config["instituicoes"]]
        with st.form("form_fixo_add"):
            cf1, cf2, cf3 = st.columns(3)
            with cf1:
                fixo_estab  = st.text_input("Estabelecimento")
                fixo_desc   = st.text_input("Descrição")
                fixo_valor  = st.number_input("Valor (R$)", min_value=0.01, step=0.01, format="%.2f")
                fixo_cat    = st.text_input("Categoria")
                fixo_subcat = st.text_input("Subcategoria")
            with cf2:
                fixo_por     = st.selectbox("Comprado por",      [""] + _compradores_para)
                fixo_para    = st.selectbox("Comprado para",     [""] + _compradores_para)
                fixo_tp      = st.selectbox("Tipo de pagamento", [""] + TIPOS_PAGAMENTO_LISTA)
                fixo_per     = st.selectbox("Periodicidade",     [""] + [p for p in PERIODICIDADES if p])
                fixo_imp     = st.selectbox("Importância",       [""] + [i for i in IMPORTANCIAS if i])
            with cf3:
                fixo_cartao  = st.selectbox("Cartão/Banco", [""] + _nomes_cartoes_fixo)
                fixo_gatilho = st.selectbox("Gatilho", ["extrato_itau", "sempre"],
                                            help="extrato_itau: só gerado quando o extrato do Itaú é processado. sempre: gerado em todo processamento.")
            ok_fixo = st.form_submit_button("➕ Incluir")
        if ok_fixo:
            if not fixo_estab.strip():
                st.warning("Estabelecimento é obrigatório.")
            else:
                try:
                    cod_tp_fixo = fixo_tp[:2] if fixo_tp else None
                    supabase.rpc("insert_lancamento_fixo", {
                        "p_familia_id":     FAMILIA_ID,
                        "p_estabelecimento": fixo_estab.strip(),
                        "p_descricao":      fixo_desc.strip() or None,
                        "p_valor_parcela":  float(fixo_valor),
                        "p_categoria":      fixo_cat.strip() or None,
                        "p_subcategoria":   fixo_subcat.strip() or None,
                        "p_comprado_por":   fixo_por or None,
                        "p_comprado_para":  fixo_para or None,
                        "p_tipo_pagamento": cod_tp_fixo,
                        "p_periodicidade":  fixo_per or None,
                        "p_importancia":    fixo_imp or None,
                        "p_cartao":         fixo_cartao or None,
                        "p_gatilho":        fixo_gatilho,
                    }).execute()
                    st.cache_data.clear()
                    st.success(f'Lançamento fixo "{fixo_estab.strip()}" incluído!')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

        if fixos_db:
            st.divider()
            st.subheader("Excluir lançamento fixo")
            fixo_opts = {f"{f['descricao'] or f['estabelecimento']} (R$ {float(f['valor_parcela']):,.2f})": f["id"] for f in fixos_db}
            fixo_del  = st.selectbox("Selecione o lançamento fixo a excluir", list(fixo_opts.keys()), key="fixo_del_sel")
            if st.button("🗑️ Excluir lançamento fixo selecionado", key="btn_fixo_del"):
                try:
                    supabase.rpc("delete_lancamento_fixo", {"p_id": fixo_opts[fixo_del], "p_familia_id": FAMILIA_ID}).execute()
                    st.cache_data.clear()
                    st.success(f'Lançamento fixo "{fixo_del}" excluído.')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ----------------------------------------------------------------
    # ABA 4 — MAPEAMENTO DE COMPRADORES
    # ----------------------------------------------------------------
    with abas_cfg[3]:
        st.subheader("Mapeamento de compradores (nome na fatura → membro)")
        st.caption("Usado para identificar quem fez a compra quando a fatura lista nomes de portadores.")

        mapas_db = sorted(_config["mapeamentos"], key=lambda m: (m.get("cartao", ""), m.get("nome_na_fatura", "")))

        if mapas_db:
            df_map = pd.DataFrame(mapas_db)[["cartao", "nome_na_fatura", "nome_membro"]]
            df_map.columns = ["Cartão", "Nome na Fatura", "Membro"]
            st.dataframe(df_map, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum mapeamento cadastrado.")

        st.divider()
        st.subheader("Incluir novo mapeamento")
        _nomes_cartoes_map = [i["nome"] for i in _config["instituicoes"] if i["tipo"] == "fatura"]
        with st.form("form_map_add"):
            cm1, cm2, cm3 = st.columns(3)
            with cm1:
                map_cartao = st.selectbox("Cartão", _nomes_cartoes_map if _nomes_cartoes_map else ["(nenhum)"])
            with cm2:
                map_fatura = st.text_input("Nome como aparece na fatura", help="Ex: FABRICIO GONCALVES")
            with cm3:
                map_membro = st.selectbox("Membro da família", _nomes_membros)
            ok_map = st.form_submit_button("➕ Incluir")
        if ok_map:
            if not map_fatura.strip():
                st.warning("Informe o nome como aparece na fatura.")
            else:
                try:
                    supabase.rpc("insert_mapeamento_comprador", {
                        "p_familia_id":     FAMILIA_ID,
                        "p_cartao":         map_cartao,
                        "p_nome_na_fatura": map_fatura.strip().upper(),
                        "p_nome_membro":    map_membro,
                    }).execute()
                    st.cache_data.clear()
                    st.success(f'Mapeamento "{map_fatura.strip().upper()}" → "{map_membro}" incluído!')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

        if mapas_db:
            st.divider()
            st.subheader("Excluir mapeamento")
            mapa_opts = {f"{m['cartao']} | {m['nome_na_fatura']} → {m['nome_membro']}": m["id"] for m in mapas_db}
            mapa_del  = st.selectbox("Selecione o mapeamento a excluir", list(mapa_opts.keys()), key="mapa_del_sel")
            if st.button("🗑️ Excluir mapeamento selecionado", key="btn_mapa_del"):
                try:
                    supabase.rpc("delete_mapeamento_comprador", {"p_id": mapa_opts[mapa_del], "p_familia_id": FAMILIA_ID}).execute()
                    st.cache_data.clear()
                    st.success("Mapeamento excluído.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ----------------------------------------------------------------
    # ABA 5 — ESTABELECIMENTOS IGNORADOS
    # ----------------------------------------------------------------
    with abas_cfg[4]:
        st.subheader("Estabelecimentos ignorados no processamento")
        st.caption("Lançamentos cujo estabelecimento contiver um desses textos serão pulados ao importar arquivos.")

        ign_db = sorted(_config["ignorados"], key=lambda i: (i.get("tipo", ""), i.get("texto", "")))

        if ign_db:
            df_ign = pd.DataFrame(ign_db)[["texto", "tipo"]]
            df_ign.columns = ["Texto", "Tipo"]
            st.dataframe(df_ign, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum estabelecimento ignorado cadastrado.")

        st.divider()
        st.subheader("Incluir novo item ignorado")
        with st.form("form_ign_add"):
            ci1_ign, ci2_ign = st.columns(2)
            with ci1_ign:
                ign_texto = st.text_input("Texto a ignorar", help="Ex: PAG BOLETO SUL AMERICA")
            with ci2_ign:
                ign_tipo  = st.selectbox("Tipo de arquivo", ["fatura", "extrato"])
            ok_ign = st.form_submit_button("➕ Incluir")
        if ok_ign:
            if not ign_texto.strip():
                st.warning("Informe o texto a ignorar.")
            else:
                try:
                    supabase.rpc("insert_estabelecimento_ignorado", {
                        "p_familia_id": FAMILIA_ID,
                        "p_texto":      ign_texto.strip(),
                        "p_tipo":       ign_tipo,
                    }).execute()
                    st.cache_data.clear()
                    st.success(f'Texto "{ign_texto.strip()}" adicionado aos ignorados.')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

        if ign_db:
            st.divider()
            st.subheader("Excluir item ignorado")
            ign_opts = {f"{i['texto']} ({i['tipo']})": i["id"] for i in ign_db}
            ign_del  = st.selectbox("Selecione o item a excluir", list(ign_opts.keys()), key="ign_del_sel")
            if st.button("🗑️ Excluir item selecionado", key="btn_ign_del"):
                try:
                    supabase.rpc("delete_estabelecimento_ignorado", {"p_id": ign_opts[ign_del], "p_familia_id": FAMILIA_ID}).execute()
                    st.cache_data.clear()
                    st.success("Item excluído.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ----------------------------------------------------------------
    # ABA 6 — CATEGORIA PADRÃO POR COMPRADOR
    # ----------------------------------------------------------------
    with abas_cfg[5]:
        st.subheader("Categoria padrão por comprador")
        st.caption(
            "Quando um lançamento não tiver categoria identificada pelas regras de classificação, "
            "o sistema usará a categoria padrão configurada aqui para o campo **Comprado Para**."
        )

        cat_pad_db = sorted(_config.get("categorias_padrao", []), key=lambda x: x.get("comprado_para", ""))

        if cat_pad_db:
            df_cp = pd.DataFrame(cat_pad_db)[["comprado_para", "categoria", "subcategoria"]]
            df_cp.columns = ["Comprado Para", "Categoria Padrão", "Subcategoria Padrão"]
            st.dataframe(df_cp, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhuma categoria padrão configurada. Lançamentos sem regra ficarão como 'Outros'.")

        st.divider()
        st.subheader("Incluir / atualizar categoria padrão")
        st.caption("Se já existir uma configuração para o comprador selecionado, ela será substituída.")
        with st.form("form_catpad_add"):
            cpp1, cpp2, cpp3 = st.columns(3)
            with cpp1:
                cp_comprador = st.selectbox("Comprado para", _compradores_para)
            with cpp2:
                cp_categoria = st.text_input("Categoria padrão", help="Ex: Compras Pessoais")
            with cpp3:
                cp_subcat    = st.text_input("Subcategoria padrão", help="Ex: Fabrício")
            ok_cp = st.form_submit_button("💾 Salvar")
        if ok_cp:
            if not cp_categoria.strip() or not cp_subcat.strip():
                st.warning("Categoria e Subcategoria são obrigatórias.")
            else:
                try:
                    supabase.rpc("insert_categoria_padrao_membro", {
                        "p_familia_id":    FAMILIA_ID,
                        "p_comprado_para": cp_comprador,
                        "p_categoria":     cp_categoria.strip(),
                        "p_subcategoria":  cp_subcat.strip(),
                    }).execute()
                    st.cache_data.clear()
                    st.success(f'Categoria padrão de "{cp_comprador}" salva: {cp_categoria.strip()} / {cp_subcat.strip()}')
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

        if cat_pad_db:
            st.divider()
            st.subheader("Excluir categoria padrão")
            cp_opts = {f"{c['comprado_para']} → {c['categoria']} / {c['subcategoria']}": c["id"] for c in cat_pad_db}
            cp_del  = st.selectbox("Selecione o item a excluir", list(cp_opts.keys()), key="cp_del_sel")
            if st.button("🗑️ Excluir categoria padrão selecionada", key="btn_cp_del"):
                try:
                    supabase.rpc("delete_categoria_padrao_membro", {"p_id": cp_opts[cp_del], "p_familia_id": FAMILIA_ID}).execute()
                    st.cache_data.clear()
                    st.success("Categoria padrão excluída.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

    # ----------------------------------------------------------------
    # ABA 7 — USUÁRIOS AUTORIZADOS
    # ----------------------------------------------------------------
    with abas_cfg[6]:
        st.subheader("Usuários autorizados")
        st.caption("Pessoas que podem acessar o app. Cada usuário precisa criar uma conta na aba Cadastrar e enviar o UID ao administrador.")

        try:
            res_usr = supabase.rpc("list_usuarios_familia", {"p_familia_id": FAMILIA_ID}).execute()
            usuarios_db = res_usr.data or []
        except Exception as e:
            st.error(f"Erro ao carregar usuários: {e}")
            usuarios_db = []

        if usuarios_db:
            df_usr = pd.DataFrame(usuarios_db)[["nome", "auth_user_id", "created_at"]]
            df_usr.columns = ["Nome", "User UID", "Adicionado em"]
            df_usr["Adicionado em"] = pd.to_datetime(df_usr["Adicionado em"]).dt.strftime("%d/%m/%Y")
            st.dataframe(df_usr, use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum usuário cadastrado.")

        st.divider()
        st.subheader("Autorizar novo usuário")
        st.caption("Cole o UID que o usuário recebeu ao criar a conta.")
        with st.form("form_usr_add"):
            cu1, cu2 = st.columns(2)
            with cu1:
                usr_nome = st.text_input("Nome (para identificação)", help="Ex: Fabiana")
            with cu2:
                usr_uid  = st.text_input("User UID", help="UUID gerado no cadastro")
            ok_usr = st.form_submit_button("➕ Autorizar")
        if ok_usr:
            if not usr_uid.strip():
                st.warning("Informe o User UID.")
            else:
                try:
                    import uuid as _uuid
                    _uuid.UUID(usr_uid.strip())  # valida formato
                    supabase.rpc("add_usuario_familia", {
                        "p_familia_id":   FAMILIA_ID,
                        "p_auth_user_id": usr_uid.strip(),
                        "p_nome":         usr_nome.strip() or None,
                    }).execute()
                    st.success(f"Usuário autorizado com sucesso!")
                    st.rerun()
                except ValueError:
                    st.error("UID inválido — deve ser um UUID no formato xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.")
                except Exception as e:
                    st.error(f"Erro: {e}")

        if usuarios_db:
            st.divider()
            st.subheader("Revogar acesso")
            usr_opts = {
                f"{u.get('nome') or 'Sem nome'} — {u['auth_user_id']}": u["id"]
                for u in usuarios_db
            }
            usr_del = st.selectbox("Selecione o usuário", list(usr_opts.keys()), key="usr_del_sel")
            st.warning("⚠️ O usuário perderá acesso imediatamente.")
            if st.button("🗑️ Revogar acesso", key="btn_usr_del"):
                try:
                    supabase.rpc("remove_usuario_familia", {
                        "p_id":         usr_opts[usr_del],
                        "p_familia_id": FAMILIA_ID,
                    }).execute()
                    st.success("Acesso revogado.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro: {e}")

# =============================================================
# PÁGINA 7 — MANUTENÇÃO
# =============================================================
elif pagina == "🔧 Manutenção":
    st.header("🔧 Manutenção")

    abas_man = st.tabs(["🗑️ Apagar Lançamentos", "📋 Templates"])

    with abas_man[0]:
        st.subheader("Apagar base de lançamentos")
        st.caption("Remove todos os lançamentos da família. Use para começar do zero ou corrigir importações incorretas.")

        try:
            res_apagar = supabase.table("lancamentos").select("id", count="exact").eq("familia_id", FAMILIA_ID).execute()
            total_lanc = res_apagar.count or 0
        except Exception as e:
            st.error(f"Erro ao contar lançamentos: {e}")
            total_lanc = None

        if total_lanc is not None:
            if total_lanc == 0:
                st.success("✅ A base de lançamentos já está vazia.")
            else:
                st.metric("Lançamentos na base", f"{total_lanc:,}".replace(",", "."))
                st.divider()
                st.error("⚠️ **ATENÇÃO:** Esta ação apagará permanentemente **todos** os lançamentos desta família. Não há como desfazer.")
                confirmar_apagar = st.checkbox(
                    "Entendo que esta ação é irreversível e desejo continuar",
                    key="chk_apagar_lanc",
                )
                if confirmar_apagar:
                    if st.button("🗑️ Apagar todos os lançamentos", type="primary", key="btn_apagar_lanc"):
                        try:
                            supabase.table("lancamentos").delete().eq("familia_id", FAMILIA_ID).execute()
                            st.success(f"✅ {total_lanc} lançamento(s) apagado(s) com sucesso.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Erro: {e}")

    with abas_man[1]:
        st.subheader("Templates de Categorias e Regras de Classificação")
        st.caption("Crie um ponto de partida para categorias e regras. Disponível somente quando a base de lançamentos está vazia.")

        try:
            res_tpl_chk = supabase.table("lancamentos").select("id", count="exact").eq("familia_id", FAMILIA_ID).execute()
            total_tpl = res_tpl_chk.count or 0
        except Exception as e:
            st.error(f"Erro ao verificar base de lançamentos: {e}")
            total_tpl = -1

        if total_tpl > 0:
            st.warning(
                f"⚠️ A base de lançamentos contém **{total_tpl} registro(s)**. "
                "Apague todos os lançamentos na aba **🗑️ Apagar Lançamentos** antes de aplicar um template."
            )
        elif total_tpl == 0:
            _TEMPLATE_CATS = [
                ("Alimentação",      "Mercado"),
                ("Alimentação",      "Restaurante"),
                ("Alimentação",      "Delivery"),
                ("Alimentação",      "Padaria / Café"),
                ("Saúde",            "Farmácia"),
                ("Saúde",            "Consulta Médica"),
                ("Saúde",            "Plano de Saúde"),
                ("Saúde",            "Exames e Laboratório"),
                ("Transporte",       "Combustível"),
                ("Transporte",       "Aplicativo (Uber/99)"),
                ("Transporte",       "Estacionamento e Pedágio"),
                ("Transporte",       "Manutenção Veicular"),
                ("Moradia",          "Condomínio"),
                ("Moradia",          "Energia Elétrica"),
                ("Moradia",          "Água e Esgoto"),
                ("Moradia",          "Internet e TV"),
                ("Moradia",          "Manutenção e Reforma"),
                ("Lazer",            "Streaming"),
                ("Lazer",            "Cinema e Teatro"),
                ("Lazer",            "Viagem e Hospedagem"),
                ("Lazer",            "Esporte e Academia"),
                ("Educação",         "Escola e Faculdade"),
                ("Educação",         "Cursos e Livros"),
                ("Vestuário",        "Roupas"),
                ("Vestuário",        "Calçados e Acessórios"),
                ("Compras Online",   "Amazon"),
                ("Compras Online",   "Shopee / Mercado Livre"),
                ("Compras Pessoais", "Diversos"),
                ("Serviços",         "Assinaturas"),
                ("Serviços",         "Serviços Domésticos"),
                ("Finanças",         "Tarifas Bancárias"),
                ("Finanças",         "Investimentos"),
                ("Outros",           "A Classificar"),
            ]

            _TEMPLATE_REGRAS = [
                # (palavra_chave, categoria, subcategoria, prioridade)
                ("SUPERMERCADO",     "Alimentação",      "Mercado",                   5),
                ("CARREFOUR",        "Alimentação",      "Mercado",                   6),
                ("PAO DE ACUCAR",    "Alimentação",      "Mercado",                   6),
                ("EXTRA HIPER",      "Alimentação",      "Mercado",                   6),
                ("ATACADAO",         "Alimentação",      "Mercado",                   6),
                ("MERCADO",          "Alimentação",      "Mercado",                   3),
                ("IFOOD",            "Alimentação",      "Delivery",                  9),
                ("RAPPI",            "Alimentação",      "Delivery",                  9),
                ("UBER EATS",        "Alimentação",      "Delivery",                  9),
                ("RESTAURANTE",      "Alimentação",      "Restaurante",               4),
                ("LANCHONETE",       "Alimentação",      "Restaurante",               4),
                ("PIZZARIA",         "Alimentação",      "Restaurante",               5),
                ("BURGER",           "Alimentação",      "Restaurante",               5),
                ("MC DONALDS",       "Alimentação",      "Restaurante",               7),
                ("SUBWAY",           "Alimentação",      "Restaurante",               7),
                ("DROGASIL",         "Saúde",            "Farmácia",                  8),
                ("DROGA RAIA",       "Saúde",            "Farmácia",                  8),
                ("ULTRAFARMA",       "Saúde",            "Farmácia",                  8),
                ("FARMACIA",         "Saúde",            "Farmácia",                  5),
                ("DROGARIA",         "Saúde",            "Farmácia",                  5),
                ("SHELL",            "Transporte",       "Combustível",               8),
                ("IPIRANGA",         "Transporte",       "Combustível",               8),
                ("BR DISTRIBUIDORA", "Transporte",       "Combustível",               8),
                ("POSTO ",           "Transporte",       "Combustível",               4),
                ("UBER",             "Transporte",       "Aplicativo (Uber/99)",      8),
                ("99APP",            "Transporte",       "Aplicativo (Uber/99)",      8),
                ("CONDOMINIO",       "Moradia",          "Condomínio",                7),
                ("CEMIG",            "Moradia",          "Energia Elétrica",          9),
                ("COPEL",            "Moradia",          "Energia Elétrica",          9),
                ("ENEL",             "Moradia",          "Energia Elétrica",          9),
                ("SABESP",           "Moradia",          "Água e Esgoto",             9),
                ("SANEPAR",          "Moradia",          "Água e Esgoto",             9),
                ("CLARO",            "Moradia",          "Internet e TV",             7),
                ("VIVO",             "Moradia",          "Internet e TV",             7),
                ("TIM ",             "Moradia",          "Internet e TV",             7),
                ("OI ",              "Moradia",          "Internet e TV",             7),
                ("NETFLIX",          "Lazer",            "Streaming",                 9),
                ("SPOTIFY",          "Lazer",            "Streaming",                 9),
                ("AMAZON PRIME",     "Lazer",            "Streaming",                 9),
                ("DISNEY",           "Lazer",            "Streaming",                 9),
                ("HBO MAX",          "Lazer",            "Streaming",                 9),
                ("APPLE TV",         "Lazer",            "Streaming",                 9),
                ("YOUTUBE PREMIUM",  "Lazer",            "Streaming",                 9),
                ("SMARTFIT",         "Lazer",            "Esporte e Academia",        9),
                ("ACADEMIA",         "Lazer",            "Esporte e Academia",        6),
                ("ESCOLA",           "Educação",         "Escola e Faculdade",        6),
                ("FACULDADE",        "Educação",         "Escola e Faculdade",        6),
                ("UNIVERS",          "Educação",         "Escola e Faculdade",        6),
                ("AMAZON",           "Compras Online",   "Amazon",                    7),
                ("SHOPEE",           "Compras Online",   "Shopee / Mercado Livre",    8),
                ("MERCADO LIVRE",    "Compras Online",   "Shopee / Mercado Livre",    8),
                ("ANUIDADE",         "Finanças",         "Tarifas Bancárias",         7),
                ("TARIFA",           "Finanças",         "Tarifas Bancárias",         5),
            ]

            t_auto, t_manual, t_salvar = st.tabs(["⚡ Template Automático", "✏️ Template Manual", "💾 Salvar Configuração como Template"])

            with t_auto:
                st.markdown(
                    "O sistema aplicará automaticamente o conjunto de categorias e regras sugerido abaixo. "
                    "Você poderá ajustar qualquer item depois nas páginas de **Regras de Classificação** e **Configurações**."
                )

                with st.expander(f"📂 Ver {len(_TEMPLATE_CATS)} categorias que serão criadas"):
                    for cat, sub in _TEMPLATE_CATS:
                        st.markdown(f"- **{cat}** / {sub}")

                with st.expander(f"📋 Ver {len(_TEMPLATE_REGRAS)} regras que serão criadas"):
                    for pk, cat, sub, pri in _TEMPLATE_REGRAS:
                        st.markdown(f"- `{pk}` → **{cat}** / {sub} *(prioridade {pri})*")

                st.divider()
                confirmar_tpl = st.checkbox(
                    "Confirmo que desejo aplicar o template automático",
                    key="chk_tpl_auto",
                )
                if confirmar_tpl:
                    if st.button("⚡ Aplicar template automático", type="primary", key="btn_tpl_auto"):
                        erros_tpl = []
                        try:
                            cat_records = [{"categoria": c, "subcategoria": s} for c, s in _TEMPLATE_CATS]
                            supabase.table("categorias").insert(cat_records).execute()
                        except Exception as e:
                            erros_tpl.append(f"Categorias: {e}")
                        try:
                            reg_records = [
                                {
                                    "palavra_chave": pk,
                                    "categoria":     cat,
                                    "subcategoria":  sub,
                                    "prioridade":    pri,
                                    "familia_id":    FAMILIA_ID,
                                }
                                for pk, cat, sub, pri in _TEMPLATE_REGRAS
                            ]
                            supabase.table("regras_classificacao").insert(reg_records).execute()
                        except Exception as e:
                            erros_tpl.append(f"Regras: {e}")

                        if erros_tpl:
                            for err in erros_tpl:
                                st.error(f"Erro: {err}")
                        else:
                            st.success(
                                f"✅ Template aplicado com sucesso: "
                                f"{len(_TEMPLATE_CATS)} categorias e {len(_TEMPLATE_REGRAS)} regras criadas."
                            )
                            st.cache_data.clear()
                            st.rerun()

            with t_manual:
                st.markdown(
                    "Adicione manualmente as categorias e regras que deseja usar. "
                    "Comece pelas categorias — as regras dependem delas."
                )

                col_cat_m, col_reg_m = st.columns(2)

                with col_cat_m:
                    st.subheader("Categorias / Subcategorias")
                    with st.form("form_tpl_cat"):
                        tpl_cat = st.text_input("Categoria", help="Ex: Alimentação")
                        tpl_sub = st.text_input("Subcategoria", help="Ex: Mercado")
                        ok_tpl_cat = st.form_submit_button("➕ Adicionar")
                    if ok_tpl_cat:
                        if not tpl_cat.strip() or not tpl_sub.strip():
                            st.warning("Preencha categoria e subcategoria.")
                        else:
                            try:
                                supabase.table("categorias").insert({
                                    "categoria":    tpl_cat.strip(),
                                    "subcategoria": tpl_sub.strip(),
                                }).execute()
                                st.success(f'"{tpl_cat.strip()} / {tpl_sub.strip()}" adicionada.')
                                st.rerun()
                            except Exception as e:
                                st.error(f"Erro: {e}")

                with col_reg_m:
                    st.subheader("Regras de Classificação")
                    cats_atuais_tpl = buscar_categorias_db()
                    opcoes_cat_tpl  = sorted(
                        set(f"{c['categoria']} / {c['subcategoria']}" for c in cats_atuais_tpl)
                    ) if cats_atuais_tpl else []

                    if not opcoes_cat_tpl:
                        st.info("Adicione ao menos uma categoria antes de criar regras.")
                    else:
                        with st.form("form_tpl_reg"):
                            tpl_pk     = st.text_input("Palavra-chave", help="Ex: IFOOD")
                            tpl_catsel = st.selectbox("Categoria / Subcategoria", opcoes_cat_tpl)
                            tpl_pri    = st.slider("Prioridade", 1, 10, 5)
                            ok_tpl_reg = st.form_submit_button("➕ Adicionar")
                        if ok_tpl_reg:
                            if not tpl_pk.strip():
                                st.warning("Informe a palavra-chave.")
                            else:
                                cat_parts = tpl_catsel.split(" / ", 1)
                                try:
                                    supabase.table("regras_classificacao").insert({
                                        "palavra_chave": tpl_pk.strip().upper(),
                                        "categoria":     cat_parts[0],
                                        "subcategoria":  cat_parts[1] if len(cat_parts) > 1 else "",
                                        "prioridade":    tpl_pri,
                                        "familia_id":    FAMILIA_ID,
                                    }).execute()
                                    st.success(f'Regra "{tpl_pk.strip().upper()}" adicionada.')
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Erro: {e}")

            with t_salvar:
                st.markdown(
                    "Gera um arquivo JSON com todas as categorias e regras de classificação "
                    "atualmente configuradas para esta família. "
                    "Use para guardar um ponto de restauração ou para aplicar em outro ambiente."
                )

                import json as _json
                from datetime import datetime as _dt_tpl

                cats_salvar = buscar_categorias_db()
                regs_salvar = buscar_regras_db()

                if not cats_salvar and not regs_salvar:
                    st.info("Não há categorias ou regras configuradas para salvar como template.")
                else:
                    c_s1, c_s2 = st.columns(2)
                    with c_s1:
                        st.metric("Categorias", len(cats_salvar))
                    with c_s2:
                        st.metric("Regras de Classificação", len(regs_salvar))

                    with st.expander(f"📂 Ver {len(cats_salvar)} categorias"):
                        for c in sorted(cats_salvar, key=lambda x: (x.get("categoria", ""), x.get("subcategoria", ""))):
                            st.markdown(f"- **{c['categoria']}** / {c['subcategoria']}")

                    with st.expander(f"📋 Ver {len(regs_salvar)} regras"):
                        for r in sorted(regs_salvar, key=lambda x: (x.get("categoria", ""), x.get("palavra_chave", ""))):
                            st.markdown(
                                f"- `{r['palavra_chave']}` → **{r['categoria']}** / "
                                f"{r.get('subcategoria', '')} *(prioridade {r.get('prioridade', 0)})*"
                            )

                    template_json = _json.dumps(
                        {
                            "nome":       f"Template — {_dt_tpl.now().strftime('%Y-%m-%d')}",
                            "gerado_em":  _dt_tpl.now().isoformat(),
                            "categorias": [
                                {"categoria": c["categoria"], "subcategoria": c["subcategoria"]}
                                for c in cats_salvar
                            ],
                            "regras": [
                                {
                                    "palavra_chave": r["palavra_chave"],
                                    "categoria":     r["categoria"],
                                    "subcategoria":  r.get("subcategoria", ""),
                                    "prioridade":    r.get("prioridade", 0),
                                }
                                for r in regs_salvar
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )

                    st.divider()
                    st.download_button(
                        label="💾 Baixar template como JSON",
                        data=template_json,
                        file_name=f"template_{_dt_tpl.now().strftime('%Y%m%d')}.json",
                        mime="application/json",
                        type="primary",
                    )