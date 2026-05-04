-- Suporte a múltiplos usuários por família
-- Execute no SQL Editor do Supabase (já aplicado via MCP em 2026-05-04)

CREATE TABLE IF NOT EXISTS familia_usuarios (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    familia_id   uuid NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    auth_user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    nome         text,
    created_at   timestamptz DEFAULT now(),
    UNIQUE(familia_id, auth_user_id)
);

INSERT INTO familia_usuarios (familia_id, auth_user_id)
SELECT id, auth_user_id FROM familias WHERE auth_user_id IS NOT NULL
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION get_familia_id()
RETURNS uuid LANGUAGE sql SECURITY DEFINER STABLE AS $$
    SELECT familia_id FROM familia_usuarios WHERE auth_user_id = auth.uid() LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION get_familia_id_by_user(p_user_id uuid)
RETURNS uuid LANGUAGE sql SECURITY DEFINER STABLE AS $$
    SELECT familia_id FROM familia_usuarios WHERE auth_user_id = p_user_id LIMIT 1;
$$;

ALTER TABLE familia_usuarios ENABLE ROW LEVEL SECURITY;
CREATE POLICY "familia_usuarios_pol" ON familia_usuarios
    FOR ALL TO authenticated USING (familia_id = get_familia_id());

CREATE OR REPLACE FUNCTION add_usuario_familia(p_familia_id uuid, p_auth_user_id uuid, p_nome text DEFAULT NULL)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO familia_usuarios (familia_id, auth_user_id, nome)
    VALUES (p_familia_id, p_auth_user_id, p_nome)
    ON CONFLICT (familia_id, auth_user_id) DO UPDATE SET nome = EXCLUDED.nome;
END;
$$;

CREATE OR REPLACE FUNCTION remove_usuario_familia(p_id uuid, p_familia_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    DELETE FROM familia_usuarios WHERE id = p_id AND familia_id = p_familia_id;
END;
$$;

CREATE OR REPLACE FUNCTION list_usuarios_familia(p_familia_id uuid)
RETURNS TABLE(id uuid, auth_user_id uuid, nome text, created_at timestamptz)
LANGUAGE sql SECURITY DEFINER STABLE AS $$
    SELECT id, auth_user_id, nome, created_at FROM familia_usuarios
    WHERE familia_id = p_familia_id ORDER BY created_at;
$$;
