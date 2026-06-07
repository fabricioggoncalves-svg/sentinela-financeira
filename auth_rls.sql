-- =============================================================
-- AUTENTICAÇÃO E ROW LEVEL SECURITY — Sentinela Financeira
--
-- Execute em DUAS ETAPAS obrigatórias (veja instruções abaixo).
-- =============================================================

-- ---------------------------------------------------------------
-- ETAPA 1 — Execute agora
-- Adiciona coluna de vínculo e cria a função auxiliar.
-- NÃO habilita RLS ainda (seu app continua funcionando).
-- ---------------------------------------------------------------

ALTER TABLE familias
    ADD COLUMN IF NOT EXISTS auth_user_id uuid REFERENCES auth.users(id);

-- Função que retorna o familia_id do usuário logado (usada pelas políticas)
CREATE OR REPLACE FUNCTION get_familia_id()
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
STABLE
AS $$
    SELECT id FROM familias WHERE auth_user_id = auth.uid() LIMIT 1;
$$;

-- ---------------------------------------------------------------
-- APÓS ETAPA 1:
-- 1. Faça seu cadastro no app (aba "Cadastrar")
-- 2. No Supabase: Authentication → Users → copie seu User UID
-- 3. Execute o UPDATE abaixo substituindo <SEU-USER-UID>:
--
--    UPDATE familias
--      SET auth_user_id = '<SEU-USER-UID>'
--      WHERE id = '11111111-1111-1111-1111-111111111111';
--
-- 4. Confirme: SELECT auth_user_id FROM familias; — deve aparecer seu UID
-- 5. Só então execute a ETAPA 2 abaixo.
-- ---------------------------------------------------------------

-- ---------------------------------------------------------------
-- ETAPA 2 — Execute SOMENTE após vincular seu auth_user_id
-- Habilita RLS e cria as políticas de isolamento por família.
-- ---------------------------------------------------------------

ALTER TABLE familias                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE membros                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE lancamentos               ENABLE ROW LEVEL SECURITY;
ALTER TABLE regras_classificacao      ENABLE ROW LEVEL SECURITY;
ALTER TABLE categorias                ENABLE ROW LEVEL SECURITY;
ALTER TABLE instituicoes_familia      ENABLE ROW LEVEL SECURITY;
ALTER TABLE mapeamento_compradores    ENABLE ROW LEVEL SECURITY;
ALTER TABLE estabelecimentos_ignorados ENABLE ROW LEVEL SECURITY;
ALTER TABLE lancamentos_fixos         ENABLE ROW LEVEL SECURITY;

-- familias: cada usuário vê apenas a sua
CREATE POLICY "familias_owner" ON familias
    FOR ALL TO authenticated
    USING (auth_user_id = auth.uid())
    WITH CHECK (auth_user_id = auth.uid());

-- Todas as outras tabelas: isoladas pelo familia_id do usuário logado
CREATE POLICY "membros_familia"       ON membros
    FOR ALL TO authenticated
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

CREATE POLICY "lancamentos_familia"   ON lancamentos
    FOR ALL TO authenticated
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

CREATE POLICY "regras_familia"        ON regras_classificacao
    FOR ALL TO authenticated
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

CREATE POLICY "categorias_familia"    ON categorias
    FOR ALL TO authenticated
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

CREATE POLICY "instituicoes_familia_pol" ON instituicoes_familia
    FOR ALL TO authenticated
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

CREATE POLICY "mapeamento_familia"    ON mapeamento_compradores
    FOR ALL TO authenticated
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

CREATE POLICY "ignorados_familia"     ON estabelecimentos_ignorados
    FOR ALL TO authenticated
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

CREATE POLICY "fixos_familia"         ON lancamentos_fixos
    FOR ALL TO authenticated
    USING  (familia_id = get_familia_id())
    WITH CHECK (familia_id = get_familia_id());

-- ---------------------------------------------------------------
-- FIM — Verifique no app: o login deve funcionar e os dados
-- da sua família devem aparecer normalmente.
-- ---------------------------------------------------------------
