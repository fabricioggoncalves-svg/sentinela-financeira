# Sentinela Financeira — Contexto do Projeto

## O que é
App Streamlit de controle financeiro familiar. Importa faturas de cartão e extratos bancários (PDF), usa Gemini para extrair lançamentos, categoriza com regras, e armazena no Supabase.

## Stack
- **Frontend:** Streamlit (Python)
- **Banco:** Supabase (PostgreSQL) com Row Level Security
- **IA:** Google Gemini 2.5 Flash (extração de PDF)
- **Arquivo principal:** `app_sentinela.py`

## Arquitetura multi-família
O app suporta múltiplos clientes (famílias). Cada família tem seus próprios dados isolados por `familia_id`.

### Tabelas principais
| Tabela | Conteúdo |
|---|---|
| `familias` | Clientes (uma por família, com `auth_user_id` para login) |
| `membros` | Pessoas da família (Fabrício, Fabiana, Gabriel...) |
| `instituicoes_familia` | Cartões e bancos configurados por família |
| `mapeamento_compradores` | Nome na fatura → membro (ex: "GABRIEL LOTT" → Gabriel) |
| `estabelecimentos_ignorados` | Textos a pular no processamento |
| `lancamentos_fixos` | Entradas mensais automáticas (planos de saúde, etc.) |
| `regras_classificacao` | Regras de categorização por palavra-chave |
| `categorias` | Pares categoria/subcategoria disponíveis |
| `lancamentos` | Lançamentos financeiros processados |

### Família atual (desenvolvimento)
- `FAMILIA_ID = '11111111-1111-1111-1111-111111111111'`
- Nome: Família Gonçalves (Fabrício, Fabiana, Gabriel, Fernanda, Ana Maria)

## Autenticação
- Supabase Auth (email + senha)
- Login → RPC `get_familia_id_by_user(p_user_id)` → resolve `familia_id`
- `familia_id` fica em `st.session_state["familia_id"]`
- Token salvo em `st.session_state["sb_access_token"]`

## Funções SQL críticas (SECURITY DEFINER)
- `get_familia_id_by_user(p_user_id uuid)` → uuid: busca familia pelo auth_user_id
- `get_config_familia(p_familia_id uuid)` → json: retorna toda config da família (bypassa RLS)
- `get_familia_id()` → uuid: retorna familia_id do usuário logado (usada nas policies RLS)

## Config carregada via RPC (não via query direta)
`buscar_config_familia(familia_id)` usa `get_config_familia` RPC porque RLS bloqueia queries diretas com anon key.

## Estado atual do RLS
- `familias`: RLS ativo (policy por auth_user_id)
- `lancamentos`, `regras_classificacao`, `categorias`: RLS parcialmente ativo (verificar)
- Config tables (`membros`, `instituicoes_familia`, etc.): RLS ativo, acessíveis via RPC SECURITY DEFINER

## O que foi implementado
- [x] Fase 1: Autenticação com Supabase Auth
- [x] Fase 3: Migração de hardcoded para banco (instituições, mapeamentos, fixos, ignorados)
- [x] Fase 4: Tela de Configurações — 6 abas: Membros, Instituições, Lançamentos Fixos, Mapeamento de Compradores, Estabelecimentos Ignorados, Categoria Padrão por Comprador (linhas 2430–2819 do app_sentinela.py)

## Limitações conhecidas da Tela de Configurações
- Apenas incluir e excluir — não há edição de registros existentes
- Onboarding de nova família requer SQL manual (não há UI para criar família)

## Arquivos SQL de referência
- `migracao_multi_familia.sql` — criação das tabelas novas
- `inserir_dados_familia.sql` — dados da família Gonçalves
- `auth_rls.sql` — RLS e políticas de segurança

## MCPs configurados
- **supabase** (HTTP): executa SQL e queries direto no Supabase
- **playwright** (stdio): testa o app no browser
- **jina** (HTTP): busca web e screenshots

## Como rodar
```bash
streamlit run app_sentinela.py
```
App sobe em `http://localhost:850X` (porta varia se a anterior estiver ocupada).
