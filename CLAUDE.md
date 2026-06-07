# Controle Familiar — Contexto do Projeto

## O que é
Sistema de controle familiar com múltiplos módulos. O módulo financeiro (Sentinela) importa faturas de cartão e extratos bancários (PDF), usa Gemini para extrair lançamentos, categoriza com regras e armazena no Supabase. Os módulos de Agenda e E-mails integram Google Calendar e Gmail. Notificações via WhatsApp (Z-API).

## Stack
- **Frontend:** Streamlit 1.54.0 (Python)
- **Banco:** Supabase (PostgreSQL) com Row Level Security
- **IA:** Google Gemini 2.5 Flash (extração de PDF)
- **Agenda/Email:** Google Calendar API + Gmail API (OAuth2)
- **WhatsApp:** Z-API (não-oficial, via QR Code)
- **Arquivo principal:** `app_sentinela.py`

## Como rodar
```bash
streamlit run app_sentinela.py
# Para acesso na rede local (celular, etc.):
streamlit run app_sentinela.py --server.address 0.0.0.0
```
App sobe em `http://localhost:8501` — acesso na rede: `http://192.168.15.97:8501`

---

## Arquitetura multi-família
O app suporta múltiplos clientes (famílias). Cada família tem dados isolados por `familia_id`.

### Tabelas principais
| Tabela | Conteúdo |
|---|---|
| `familias` | Clientes (uma por família, com `auth_user_id`) |
| `membros` | Pessoas da família |
| `instituicoes_familia` | Cartões e bancos por família |
| `mapeamento_compradores` | Nome na fatura → membro |
| `estabelecimentos_ignorados` | Textos a pular no processamento |
| `lancamentos_fixos` | Entradas mensais automáticas |
| `regras_classificacao` | Regras de categorização por palavra-chave |
| `categorias` | Pares categoria/subcategoria |
| `lancamentos` | Lançamentos financeiros processados |
| `integracoes_familia` | Config de integrações externas (Z-API, Google) em jsonb |

### Família de desenvolvimento
- `FAMILIA_ID = '11111111-1111-1111-1111-111111111111'`
- Membros: Fabrício, Fabiana, Gabriel, Fernanda, Ana Maria
- Auth: fabricioggoncalves@gmail.com

### Cartões configurados
| Nome | Tipo | Portadores múltiplos | Comprador fixo |
|---|---|---|---|
| BB OuroCard | fatura | não | Fabiana |
| C6 Black | fatura | sim | — |
| Master Múltiplo Black | fatura | sim | — |
| Visa Azul Infinity | fatura | não | Fabrício |
| CEF | extrato | — | — |
| Itaú | extrato | — | — |

---

## Autenticação
- Supabase Auth (email + senha)
- Login → query direta em `familias` por `auth_user_id` → resolve `familia_id`
- `familia_id` fica em `st.session_state["familia_id"]`
- Token salvo em `st.session_state["sb_access_token"]`

---

## Módulos do sistema

### 💰 Financeiro (Sentinela)
Módulo original com sub-páginas:
- 📄 Processar Arquivos
- 💳 Gerenciar Lançamentos
- 📊 Relatórios
- 📂 Categorias
- 🏷️ Regras de Classificação
- ⚙️ Configurações (família: membros, instituições, fixos, mapeamentos, ignorados)
- 🔧 Manutenção

### 📅 Agenda
- Google Calendar via OAuth2 (web credentials, redirect `http://localhost:8501/`)
- Handler de callback OAuth restaura sessão Supabase via arquivo temporário em `cf_oauth_states/`
- Lista eventos com filtro 7/14/30 dias
- Botão 📲 por evento envia lembrete via WhatsApp (se Z-API configurado)
- Credenciais: `google_credentials.json` (gitignored), token: `google_token.json` (gitignored)

### 📧 E-mails
- Painel de triagem do Gmail via Gmail API (reusa OAuth2/credenciais do Calendar — escopo `gmail.readonly`)
- Filtro por label (Caixa de entrada + labels do usuário), busca livre (`q` da API) e checkbox "Somente boletos/faturas"
- Detecção heurística de e-mails financeiros por palavra-chave no assunto/resumo (`_email_eh_financeiro`)
- Botão 📲 por e-mail encaminha assunto + remetente + resumo via WhatsApp (Z-API)
- Botão de conectar Google é compartilhado com a Agenda; ao concluir o OAuth, o usuário retorna ao módulo de origem (`modulo_origem` salvo no estado temporário)

### ⚙️ Configurações do Sistema
- **Aba Z-API:** formulário Instance ID + Token + telefone, verifica status, exibe QR Code para conectar WhatsApp, botão de teste
- **Aba Google:** mostra status de conexão e botão desconectar

---

## Funções SQL críticas (SECURITY DEFINER)
- `get_config_familia(p_familia_id uuid)` → json: retorna toda config da família (bypassa RLS)
- `get_familia_id()` → uuid: retorna familia_id do usuário logado (usada nas policies RLS)

### Problema conhecido — RLS com anon key
RLS com anon key não propaga JWT corretamente para PostgREST no supabase-py em Streamlit.
**Solução:** Usar RPC SECURITY DEFINER para queries de config. Queries de lancamentos/regras/categorias funcionam porque o token é propagado manualmente via `supabase.postgrest.auth(token)`.

---

## Processamento de faturas — comportamento esperado

### Extração de lançamentos
- **Extrato Itaú:** extração direta via `pdfplumber` (sem IA) → confiável
- **Outros extratos (CEF etc.):** texto via pdfplumber → Gemini; fallback: upload do PDF como arquivo para o Gemini
- **Faturas de cartão:** texto por bloco (2 páginas) → Gemini; fallback: upload do PDF se texto retornar 0

### Verificação de totais
O total correto a comparar **não é** o "Total da fatura" (que desconta pagamentos do mês anterior), mas sim:
- Itaú/CEF: soma dos débitos do extrato
- Cartões: **"Compras nacionais"** ou equivalente no resumo da fatura (bruto das novas compras)

Exemplo BB: fatura mostra R$ 8.768,80 (total após pagamentos), mas app deve importar R$ 9.358,30 (compras nacionais).

### Problemas corrigidos
- **Parcelas "próximas faturas":** prompt reforçado para ignorar a seção "Compras parceladas - próximas faturas"
- **Rate limit Gemini:** workers reduzidos de 5 para 2 (ThreadPoolExecutor)
- **Erros silenciosos:** se todos os blocos falharem, RuntimeError é levantado e exibido na UI
- **Deduplicação agressiva:** permite até 2 entradas idênticas por dia (compras legítimas duplicadas); remove triplicatas+
- **PDFs complexos (C6, etc.):** fallback de upload direto para Gemini quando extração por texto retorna 0

### Faturas com múltiplos portadores
- C6 Black e Master Múltiplo Black têm `tem_multiplos_portadores = true`
- Prompt identifica portador pelo cabeçalho de seção ("C6 Carbon Final XXXX - NOME")
- Estornos devem ser incluídos como valores negativos

---

## Integração Google OAuth2

### Fluxo
1. `google_iniciar_oauth()` salva estado Supabase em arquivo temporário e gera URL
2. Usuário autoriza no Google → Google redireciona para `http://localhost:8501/?code=...&state=...`
3. Handler no início do script (antes do auth check) restaura sessão Supabase + troca code por token
4. Token salvo em `google_token.json` e `st.session_state["google_token"]`
5. `st.query_params.clear()` + `st.rerun()` → volta para o módulo Agenda

### Variáveis de ambiente necessárias
```
OAUTHLIB_INSECURE_TRANSPORT=1  (HTTP local)
OAUTHLIB_RELAX_TOKEN_SCOPE=1   (Google retorna escopos extras como openid)
```

### Escopos
- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/gmail.readonly`

---

## Integração Z-API (WhatsApp)

### Config
- Tabela `integracoes_familia` → tipo `'zapi'` → config jsonb: `{instance_id, token, telefone}`
- Funções: `zapi_carregar_config`, `zapi_salvar_config`, `zapi_status`, `zapi_enviar_mensagem`
- Endpoint envio: `POST https://api.z-api.io/instances/{id}/token/{token}/send-text`

### Cache
Config Z-API é cacheada em `st.session_state["zapi_config"]`. Limpar com `st.session_state.pop("zapi_config", None)` após salvar.

---

## Arquivos importantes
| Arquivo | Descrição |
|---|---|
| `app_sentinela.py` | Arquivo principal (≈3500 linhas) |
| `google_credentials.json` | Credenciais OAuth2 do Google Cloud (gitignored) |
| `google_token.json` | Token de acesso Google após autorização (gitignored) |
| `requirements.txt` | Dependências Python com versões fixadas |
| `migrations/` | SQL migrations numeradas sequencialmente |
| `migracao_multi_familia.sql` | Criação das tabelas multi-família |
| `inserir_dados_familia.sql` | Dados da família Gonçalves |
| `auth_rls.sql` | RLS e políticas de segurança |

## Migrations aplicadas
| Arquivo | Conteúdo |
|---|---|
| `migrations/add_comprado_para_to_regras.sql` | Coluna comprado_para em regras |
| `migrations/add_familia_usuarios.sql` | Tabela familia_usuarios |
| `migrations/002_integracoes_familia.sql` | Tabela integracoes_familia (Z-API, Google) |

---

## MCPs configurados
- **supabase** (HTTP): executa SQL e queries direto no Supabase — project_id: `agvcqkjzwktdjstmhizz`
- **playwright** (stdio): testa o app no browser
- **jina** (HTTP): busca web e screenshots

---

## Próximos passos
1. **Notificações automáticas** — lembretes de eventos agendados (APScheduler ou cron)
2. **Autostart Windows** — Task Scheduler para manter o app sempre disponível
3. **Onboarding de nova família** — UI para criar família (hoje requer SQL manual)
4. **Refinar detecção de e-mails financeiros** — heurística por palavra-chave hoje; avaliar Gemini para classificar/extrair valor e vencimento de boletos
