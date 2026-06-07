-- Adiciona coluna comprado_para na tabela regras_classificacao
-- Execute no Supabase: Dashboard → SQL Editor → New Query

ALTER TABLE regras_classificacao
ADD COLUMN IF NOT EXISTS comprado_para text DEFAULT '' NOT NULL;

COMMENT ON COLUMN regras_classificacao.comprado_para IS
    'Se preenchido, propaga automaticamente o valor de "comprado_para" ao lançamento classificado por esta regra.';
