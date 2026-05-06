# [Nome do App] — Contexto do Projeto

> Template baseado no projeto Sentinela Financeira.
> Substitua os blocos entre colchetes pelo conteúdo real do seu projeto.

---

## Como usar este template

Este template é composto por dois artefatos que se complementam:

| Artefato | Função |
|---|---|
| `CLAUDE.md` (este arquivo) | Contexto arquitetural — explica o quê, o porquê e as decisões do projeto |
| `.claude/rules/*.md` | Regras operacionais — o Claude as aplica automaticamente em toda ação |

Os dois juntos eliminam a necessidade de repetir instruções a cada conversa.

### Passo a passo para um novo projeto

**1. Preparar o CLAUDE.md**
- Renomear este arquivo para `CLAUDE.md` na raiz do novo projeto.
- Preencher todos os blocos `[entre colchetes]` com os dados reais do projeto.
- Remover seções que não se aplicam (ex: se não houver PDF, remover a seção de processamento).

**2. Copiar a pasta `.claude/`**
Copiar a pasta inteira do projeto Sentinela Financeira para a raiz do novo projeto:
```
.claude/
  rules/
  commands/
  agents/
  settings.json
```

**3. Personalizar obrigatoriamente**

| Arquivo | O que trocar |
|---|---|
| `rules/db-conventions.md` | `familia_id` / `familias` → nome do tenant do novo projeto |
| `commands/dump-config.md` | UUID de dev (`11111111-...`) e nome da RPC de config |
| `commands/novo-tenant.md` | UUID de dev e nomes das tabelas principais |
| `settings.json` | Tokens reais dos MCPs (Supabase, Jina, etc.) |

**4. Reaproveitar sem alteração**

| Arquivo | Motivo |
|---|---|
| `rules/security.md` | Independente do domínio |
| `rules/streamlit-patterns.md` | Válido para qualquer app Streamlit |
| `agents/sql-reviewer.md` | Genérico para qualquer projeto Supabase |
| `agents/pdf-debugger.md` | Específico para extração de PDF com IA |

**5. Adicionar ao `.gitignore`**
```
.env
.claude/settings.local.json
```

**6. Verificar antes de começar a desenvolver**
- [ ] `CLAUDE.md` preenchido com dados reais do projeto
- [ ] UUID fixo de dev definido e inserido no banco
- [ ] MCPs configurados em `settings.json` com tokens válidos
- [ ] `.env` criado localmente com as variáveis necessárias
- [ ] `.env.example` commitado com valores fictícios

---

## O que é

[Descrição em 2–3 linhas do propósito do app: o que ele faz, para quem, e qual problema resolve.]

Exemplo: App Streamlit de controle financeiro familiar. Importa faturas de cartão e extratos bancários (PDF), usa IA para extrair lançamentos, categoriza com regras configuráveis, e armazena no Supabase.

---

## Stack

| Camada | Tecnologia |
|---|---|
| Frontend | Streamlit (Python) |
| Banco de dados | Supabase (PostgreSQL) com Row Level Security |
| IA / extração | Google Gemini 2.5 Flash (ou outro modelo) |
| Autenticação | Supabase Auth (email + senha) |
| Arquivo principal | `app_[nome].py` |

---

## Arquitetura multi-tenant

O app suporta múltiplos clientes (tenants). Cada tenant tem seus próprios dados isolados por `[tenant]_id`.

### Regra de ouro
Toda tabela com dados de usuário tem uma coluna `[tenant]_id`. Toda query filtra por ela. O RLS reforça isso no banco.

### Tabelas principais

| Tabela | Conteúdo |
|---|---|
| `[tenants]` | Clientes cadastrados, com `auth_user_id` para login |
| `[entidade_principal]` | [Descrição] |
| `[entidade_config_1]` | [Descrição] |
| `[entidade_config_2]` | [Descrição] |
| `[lancamentos]` | Registros financeiros / transacionais processados |
| `[regras]` | Regras de categorização por palavra-chave |
| `[categorias]` | Taxonomia de categorias disponíveis |

### Tenant de desenvolvimento
- `[TENANT]_ID = '[uuid-fixo-para-dev]'` (ex: `'11111111-1111-1111-1111-111111111111'`)
- Use um UUID fixo e fácil de lembrar durante o desenvolvimento.

---

## Autenticação

Fluxo:
1. Usuário faz login com email + senha via Supabase Auth.
2. Após login, chama RPC `get_[tenant]_id_by_user(p_user_id)` para resolver o `[tenant]_id`.
3. `[tenant]_id` fica em `st.session_state["[tenant]_id"]`.
4. Access token salvo em `st.session_state["sb_access_token"]`.
5. Todas as chamadas ao Supabase usam esse token.

### Padrão de verificação de sessão
Toda página/aba do Streamlit começa com:
```python
if not st.session_state.get("autenticado"):
    st.warning("Faça login para continuar.")
    st.stop()
```

---

## Funções SQL críticas (SECURITY DEFINER)

Estas funções executam com privilégios elevados, bypasando o RLS onde necessário. Devem ser criadas com cuidado e auditadas.

| Função | Retorno | Propósito |
|---|---|---|
| `get_[tenant]_id_by_user(p_user_id uuid)` | uuid | Resolve [tenant]_id pelo auth_user_id |
| `get_config_[tenant](p_[tenant]_id uuid)` | json | Retorna toda a config do tenant em uma chamada |
| `get_[tenant]_id()` | uuid | Retorna [tenant]_id do usuário logado (usada nas policies) |

### Por que usar SECURITY DEFINER para leitura de config?
O Supabase com `anon key` não propaga o JWT corretamente para o PostgREST quando chamado via `supabase-py` no Streamlit. A solução é encapsular leituras de config em RPCs SECURITY DEFINER.

**Nunca** use SECURITY DEFINER para escrita genérica — crie funções específicas por operação (insert, delete) e valide o `[tenant]_id` dentro delas.

---

## Row Level Security (RLS)

### Política padrão para tabelas de dados
```sql
-- Habilitar RLS
ALTER TABLE [tabela] ENABLE ROW LEVEL SECURITY;

-- Policy de leitura
CREATE POLICY "[tabela]_select" ON [tabela]
  FOR SELECT USING (
    [tenant]_id = get_[tenant]_id()
  );

-- Policy de escrita
CREATE POLICY "[tabela]_insert" ON [tabela]
  FOR INSERT WITH CHECK (
    [tenant]_id = get_[tenant]_id()
  );
```

### Estado esperado do RLS
- Tabelas de config (membros, regras, etc.): RLS ativo, leitura via RPC SECURITY DEFINER.
- Tabelas transacionais (lançamentos): RLS ativo com policy padrão por tenant_id.
- Tabela de tenants: RLS ativo com policy por `auth_user_id`.

### Armadilha comum
Queries diretas com `anon key` falham silenciosamente (retornam 0 linhas) quando o RLS está ativo e o JWT não é propagado. Sintoma: app parece funcionar mas não mostra dados. Diagnóstico: testar a mesma query no SQL Editor do Supabase Dashboard.

---

## Carregamento de configuração

Toda a configuração do tenant é carregada de uma vez via RPC e cacheada em `st.session_state`:

```python
@st.cache_data(ttl=300)
def buscar_config_[tenant]([tenant]_id: str) -> dict:
    resp = supabase.rpc("get_config_[tenant]", {"p_[tenant]_id": [tenant]_id}).execute()
    return resp.data or {}
```

Estrutura retornada pela RPC (JSON):
```json
{
  "[entidade_1]": [...],
  "[entidade_2]": [...],
  "regras": [...],
  "categorias": [...]
}
```

---

## Tela de Configurações

Padrão de implementação com abas no Streamlit:

```python
abas = st.tabs(["Aba 1", "Aba 2", "Aba 3", ...])

with abas[0]:
    # Listar registros existentes
    # Formulário de inclusão
    # Botão de exclusão por ID
```

### Operações suportadas por padrão
- Listar: direto da config cacheada em session_state
- Incluir: chama RPC `insert_[entidade](...)`
- Excluir: chama RPC `delete_[entidade](p_id)`
- Editar: **não implementado por padrão** — adicionar se necessário

### Padrão de RPC para escrita
```sql
CREATE OR REPLACE FUNCTION insert_[entidade](
  p_[tenant]_id uuid,
  p_campo1 text,
  p_campo2 text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  -- Validar que o tenant_id pertence ao usuário logado
  IF p_[tenant]_id != get_[tenant]_id() THEN
    RAISE EXCEPTION 'Acesso negado';
  END IF;

  INSERT INTO [entidade] ([tenant]_id, campo1, campo2)
  VALUES (p_[tenant]_id, p_campo1, p_campo2);
END;
$$;
```

---

## Processamento de PDF com IA

### Fluxo
1. Usuário faz upload do PDF.
2. PDF é convertido para base64 e enviado ao Gemini com um prompt estruturado.
3. Gemini retorna JSON com os lançamentos extraídos.
4. Lançamentos passam pelo pipeline de enriquecimento:
   - Mapeamento de comprador (nome na fatura → membro)
   - Filtro de estabelecimentos ignorados
   - Aplicação de regras de classificação (categoria/subcategoria)
   - Merge com lançamentos fixos do mês
5. Resultado exibido para revisão antes de salvar.

### Prompt para extração
O prompt deve:
- Especificar o formato JSON esperado com campos obrigatórios.
- Informar o formato de data esperado.
- Pedir para ignorar totais, subtotais e pagamentos.
- Incluir exemplo de saída esperada.

### Armadilhas de PDFs bancários
- PDFs com dupla camada (texto + imagem) podem gerar lançamentos duplicados. Usar deduplicação por `(data, estabelecimento, valor)` antes de retornar.
- Datas podem vir em formatos diferentes entre bancos (DD/MM, MM/DD, YYYY-MM-DD). Normalizar tudo para `date` Python antes de salvar.
- Valores podem ter vírgula ou ponto como separador decimal dependendo do banco.

---

## Regras de Classificação

As regras associam palavras-chave a pares categoria/subcategoria.

### Estrutura
```sql
CREATE TABLE regras_classificacao (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  [tenant]_id uuid REFERENCES [tenants](id),
  palavra_chave text NOT NULL,      -- texto a buscar no estabelecimento
  categoria text NOT NULL,
  subcategoria text,
  prioridade int DEFAULT 0,         -- maior = aplica primeiro
  comprado_para text                -- opcional: membro específico
);
```

### Lógica de aplicação
```python
def classificar(estabelecimento: str, regras: list) -> dict:
    nome = estabelecimento.upper()
    for regra in sorted(regras, key=lambda r: -r["prioridade"]):
        if regra["palavra_chave"].upper() in nome:
            return {"categoria": regra["categoria"], "subcategoria": regra["subcategoria"]}
    return {"categoria": "A Classificar", "subcategoria": ""}
```

---

## Estado de sessão (session_state) — convenção

| Chave | Tipo | Conteúdo |
|---|---|---|
| `autenticado` | bool | True após login bem-sucedido |
| `[tenant]_id` | str | UUID do tenant do usuário logado |
| `sb_access_token` | str | JWT do Supabase Auth |
| `config` | dict | Config completa do tenant (cacheada) |
| `usuario_email` | str | Email do usuário logado |

---

## Estrutura de arquivos

```
app_[nome].py          # App principal (único arquivo Streamlit)
requirements.txt       # Dependências Python com versões fixadas
runtime.txt            # Versão do Python para deploy (ex: python-3.11)
.env                   # Variáveis de ambiente locais (não commitar)
.env.example           # Exemplo de .env para documentação
migrations/            # Scripts SQL de migração numerados
  001_schema_inicial.sql
  002_rls_policies.sql
  003_funcoes_rpc.sql
  004_dados_seed.sql
CLAUDE.md              # Este arquivo
```

---

## Variáveis de ambiente

```env
SUPABASE_URL=https://[projeto].supabase.co
SUPABASE_KEY=[anon-key]
GEMINI_API_KEY=[chave-gemini]
```

No Streamlit Cloud, configurar em **Settings → Secrets** no formato TOML:
```toml
SUPABASE_URL = "https://[projeto].supabase.co"
SUPABASE_KEY = "[anon-key]"
GEMINI_API_KEY = "[chave-gemini]"
```

Leitura no código:
```python
import streamlit as st
import os

SUPABASE_URL = st.secrets.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY") or os.getenv("SUPABASE_KEY")
```

---

## MCPs recomendados para desenvolvimento

| MCP | Protocolo | Uso |
|---|---|---|
| `supabase` | HTTP | Executar SQL e inspecionar banco sem sair do Claude |
| `playwright` | stdio | Testar o app no browser, capturar screenshots |
| `jina` | HTTP | Buscar documentação, ler URLs, pesquisar na web |

---

## Fases de desenvolvimento recomendadas

### Fase 1 — Fundação
- [ ] Schema do banco (tabelas principais + tenant)
- [ ] RLS básico (habilitar, criar get_tenant_id())
- [ ] Autenticação (login/logout, session_state)
- [ ] Carregamento de config via RPC

### Fase 2 — Funcionalidade principal
- [ ] Upload e processamento de PDF com IA
- [ ] Pipeline de enriquecimento de lançamentos
- [ ] Tela de revisão antes de salvar
- [ ] Salvamento no banco com tenant_id

### Fase 3 — Configuração via UI
- [ ] Tela de Configurações com abas
- [ ] CRUD de regras de classificação
- [ ] CRUD de entidades de config (membros, instituições, etc.)
- [ ] Lançamentos fixos mensais

### Fase 4 — Relatórios e polish
- [ ] Dashboard / resumo mensal
- [ ] Filtros por período, categoria, membro
- [ ] Exportação (CSV, Excel)
- [ ] Edição de lançamentos individuais

### Fase 5 — Produção
- [ ] Deploy no Streamlit Cloud
- [ ] Onboarding de novos tenants via UI
- [ ] Testes com dados reais
- [ ] Monitoramento de erros

---

## Decisões de arquitetura e lições aprendidas

### Use UUID fixo para o tenant de desenvolvimento
Facilita scripts SQL manuais, seeds e testes. Defina no topo do app como constante.

### Nunca hardcode dados de negócio no código
Instituições, membros, regras e categorias devem estar no banco desde o início. Hardcode é dívida técnica que vai custar caro quando o segundo tenant aparecer.

### Cache a config do tenant em session_state
A config muda raramente. Carregar a cada rerun do Streamlit é lento e desnecessário. Use `@st.cache_data(ttl=300)` ou salve em `session_state` após o login.

### RLS + anon key + supabase-py = cuidado
O JWT não é propagado automaticamente para o PostgREST em todas as versões do `supabase-py`. Se queries retornam vazio quando deveriam retornar dados, o RLS está bloqueando. Solução: usar RPC SECURITY DEFINER ou checar se o token está sendo passado no header.

### Versione as dependências
No Streamlit Cloud, `requirements.txt` sem versão fixa pode quebrar o deploy quando uma biblioteca atualiza. Fixe as versões principais:
```
streamlit==1.x.x
supabase==2.x.x
google-generativeai==0.x.x
pandas==2.x.x
pdfplumber==0.x.x
```

### Um arquivo Python, múltiplas abas
Para apps Streamlit de médio porte, um único arquivo com funções bem nomeadas e seções comentadas é mais simples de manter do que múltiplos arquivos + imports. Considere separar em módulos só quando o arquivo ultrapassar ~3000 linhas.

---

## Como rodar localmente

```bash
# Instalar dependências
pip install -r requirements.txt

# Rodar o app
streamlit run app_[nome].py
```

App sobe em `http://localhost:8501` (porta incrementa se ocupada).

## Como rodar com MCPs (Claude Code)

Certifique-se de que os MCPs estão configurados em `.claude/settings.json` antes de iniciar o Claude Code. Com o MCP do Supabase ativo, você pode executar SQL diretamente sem abrir o Dashboard.

---

## Estrutura do Claude Code (`.claude/`)

Além do `CLAUDE.md`, o Claude Code reconhece arquivos em subpastas específicas:

```
.claude/
  settings.json          # MCPs, permissões, hooks (commitar)
  settings.local.json    # Overrides locais, tokens (NÃO commitar)
  rules/                 # Carregadas automaticamente em todo contexto
    db-conventions.md    # Isolamento por tenant, RLS, nomenclatura SQL
    security.md          # Credenciais, SECURITY DEFINER, inputs
    streamlit-patterns.md # Cache, rerun, formulários, deploy
  commands/              # Slash commands: /nome
    testar-rls.md        # Verifica RLS em todas as tabelas
    dump-config.md       # Exibe config do tenant formatada
    novo-tenant.md       # Gera SQL para cadastrar nova família
    checar-migracao.md   # Cruza arquivos locais com banco
  agents/                # Sub-agentes especializados
    sql-reviewer.md      # Revisa migrations antes de aplicar
    pdf-debugger.md      # Depura extração de PDFs bancários
```

### Como usar

| Artefato | Como acionar |
|---|---|
| `rules/` | Automático — carregado em toda conversa |
| `commands/` | Digitando `/nome-do-comando` no chat |
| `agents/` | Claude invoca automaticamente ou você pede: "use o pdf-debugger" |
| `settings.json` | Automático ao iniciar o Claude Code |

### `settings.json` — template para este tipo de app

```json
{
  "mcpServers": {
    "supabase": {
      "type": "http",
      "url": "https://mcp.supabase.com",
      "headers": { "Authorization": "Bearer [SUPABASE_ACCESS_TOKEN]" }
    },
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    },
    "jina": {
      "type": "http",
      "url": "https://mcp.jina.ai",
      "headers": { "Authorization": "Bearer [JINA_API_KEY]" }
    }
  },
  "permissions": {
    "allow": [
      "Bash(streamlit:*)",
      "Bash(pip:*)",
      "Bash(git log:*)",
      "Bash(git diff:*)",
      "Bash(git status:*)",
      "Bash(python -m py_compile:*)"
    ]
  }
}
```
