-- Execute este script no SQL Editor do Supabase para popular os dados da família.
-- As tabelas já existem — este script só insere os dados.

INSERT INTO familias (id, nome)
VALUES ('11111111-1111-1111-1111-111111111111', 'Família Gonçalves')
ON CONFLICT (id) DO NOTHING;

INSERT INTO membros (familia_id, nome) VALUES
    ('11111111-1111-1111-1111-111111111111', 'Fabrício'),
    ('11111111-1111-1111-1111-111111111111', 'Fabiana'),
    ('11111111-1111-1111-1111-111111111111', 'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'Fernanda'),
    ('11111111-1111-1111-1111-111111111111', 'Ana Maria');

INSERT INTO instituicoes_familia
    (familia_id, nome, chave_checklist, tipo, padroes_arquivo, comprador_fixo, comprador_padrao, tem_multiplos_portadores)
VALUES
    ('11111111-1111-1111-1111-111111111111', 'Itaú',                  'Itaú',            'extrato', ARRAY['ITAU','ITAÚ'],                  NULL,       'Fabrício', false),
    ('11111111-1111-1111-1111-111111111111', 'CEF',                   'CEF',             'extrato', ARRAY['CEF','CAIXA'],                  NULL,       'Fabrício', false),
    ('11111111-1111-1111-1111-111111111111', 'BB OuroCard',           'BB',              'fatura',  ARRAY['BB'],                           'Fabiana',  NULL,       false),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black',              'C6',              'fatura',  ARRAY['C6'],                           NULL,       NULL,       true),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'Master Múltiplo', 'fatura',  ARRAY['MASTER','MULTIPLO','MÚLTIPLO'], NULL,       NULL,       true),
    ('11111111-1111-1111-1111-111111111111', 'Visa Azul Infinity',    'Visa Azul',       'fatura',  ARRAY['VISA INFINITY','VISA AZUL'],    'Fabrício', NULL,       false);

INSERT INTO mapeamento_compradores (familia_id, cartao, nome_na_fatura, nome_membro) VALUES
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'FABRICIO GONCALVES',  'Fabrício'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'FABRICIO GUIMARAES',  'Fabrício'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'GABRIEL LOTT',        'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'GABRIELLOTT',         'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'FERNANDALOTT',        'Fernanda'),
    ('11111111-1111-1111-1111-111111111111', 'C6 Black', 'FABIANALOTT',         'Fabiana'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FABRICIO GUIMARAES', 'Fabrício'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FABIANA MORAES',     'Fabiana'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'GABRIEL LOTT',       'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FERNANDA LOTT',      'Fernanda'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'GABRIELLOTT',        'Gabriel'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FERNANDALOTT',       'Fernanda'),
    ('11111111-1111-1111-1111-111111111111', 'Master Múltiplo Black', 'FABIANALOTT',        'Fabiana');

INSERT INTO estabelecimentos_ignorados (familia_id, texto, tipo) VALUES
    ('11111111-1111-1111-1111-111111111111', 'PAG BOLETO SUL AMERICA',    'fatura'),
    ('11111111-1111-1111-1111-111111111111', 'PIX QRS SUL AMERICA',       'fatura'),
    ('11111111-1111-1111-1111-111111111111', 'OUTRAS DESPESAS ANA MARIA', 'fatura'),
    ('11111111-1111-1111-1111-111111111111', 'CREDIARIO AUTOM',           'fatura'),
    ('11111111-1111-1111-1111-111111111111', 'PIX TRANSF FABIANA',        'fatura'),
    ('11111111-1111-1111-1111-111111111111', 'PAG BOLETO BANCO C6 S.A',   'extrato'),
    ('11111111-1111-1111-1111-111111111111', 'LIQUIDA OPERACAO',          'extrato');

INSERT INTO lancamentos_fixos
    (familia_id, estabelecimento, descricao, valor_parcela, categoria, subcategoria,
     comprado_por, comprado_para, tipo_pagamento, periodicidade, importancia, cartao, gatilho)
VALUES
    ('11111111-1111-1111-1111-111111111111',
     'PAG BOLETO SUL AMERICA COMPANHIA DE SEG', 'Plano de Saúde - Fabrício',
     2272.44, 'Saúde', 'Plano de Saúde', 'Fabrício', 'Família', '06', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau'),

    ('11111111-1111-1111-1111-111111111111',
     'PAG BOLETO SUL AMERICA COMPANHIA DE SEG', 'Plano de Saúde - Fabiana',
     2272.44, 'Saúde', 'Plano de Saúde', 'Fabiana', 'Família', '06', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau'),

    ('11111111-1111-1111-1111-111111111111',
     'PAG BOLETO SUL AMERICA COMPANHIA DE SEG', 'Plano de Saúde - Fernanda',
     637.98, 'Saúde', 'Plano de Saúde', 'Fernanda', 'Família', '06', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau'),

    ('11111111-1111-1111-1111-111111111111',
     'PAG BOLETO SUL AMERICA COMPANHIA DE SEG', 'Plano de Saúde - Ana Maria',
     3827.93, 'Ana Maria', 'Plano de Saúde', 'Ana Maria', 'Família', '06', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau'),

    ('11111111-1111-1111-1111-111111111111',
     'OUTRAS DESPESAS ANA MARIA', 'Faxineira Ana Maria',
     1600.00, 'Ana Maria', 'Outras Despesas', 'Ana Maria', 'Família', '02', 'Fixa Mensal', 'Essencial', 'Itaú', 'extrato_itau');

UPDATE lancamentos        SET familia_id = '11111111-1111-1111-1111-111111111111' WHERE familia_id IS NULL;
UPDATE regras_classificacao SET familia_id = '11111111-1111-1111-1111-111111111111' WHERE familia_id IS NULL;
UPDATE categorias           SET familia_id = '11111111-1111-1111-1111-111111111111' WHERE familia_id IS NULL;
