#!/usr/bin/env python3
"""
Índice de NOMES de empresas ativas (base aberta de CNPJ da Receita Federal).

Usado pela Consulta CNPJ do D'AGILI FORCE para buscar empresa pelo nome
(razão social ou nome fantasia) sem API paga. Gera um arquivo texto
compactado, uma empresa por linha:

    CNPJ|RAZAO SOCIAL|NOME FANTASIA|MUNICIPIO|M ou F|CNAE PRINCIPAL|CNAES SECUNDARIOS|SIMPLES (S/N)

(CNAEs com 7 dígitos; secundários separados por vírgula.) Gera também a
tabela de CNAEs (código e descrição) em JSON, para a busca por atividade.

- Só estabelecimentos ATIVOS da(s) UF(s) escolhida(s) (padrão RS).
- Sem MEI (padrão), para o arquivo caber na hospedagem. Use SEM_MEI=0 para incluir.
- Nomes em maiúsculas sem acento (a busca no PHP é feita assim).

Uso:
    python indice_nomes.py --saida indice/empresas_rs.txt.gz --info indice/empresas_rs_info.json --cnaes indice/cnaes.json
    python indice_nomes.py --saida x.txt.gz --info x.json --local pasta_com_zips   (teste, sem baixar)

Variáveis opcionais: UF (padrão RS), SEM_MEI (padrão 1).
"""
import argparse
import datetime as dt
import gzip
import json
import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from corretoras_rs import Fonte, campos, linhas_zip, log, mes_mais_recente, tabela  # noqa: E402

UFS = [u.strip().upper() for u in os.getenv('UF', 'RS').split(',') if u.strip()]
SEM_MEI = os.getenv('SEM_MEI', '1') != '0'
VERSAO = 2  # formato da linha; mudar força reprocessar


def normalizar(t):
    t = unicodedata.normalize('NFKD', t or '').encode('ascii', 'ignore').decode('ascii')
    return ' '.join(t.replace('|', ' ').upper().split())


def basico_da_linha(linha):
    return linha[1:9] if linha.startswith('"') else linha[:8]


def processar(fonte, arq_cnaes=None):
    marcas = tuple(f'"{u}"' for u in UFS) + tuple(f';{u};' for u in UFS)
    municipios = tabela(fonte, 'Municipios.zip')
    if arq_cnaes:
        cnaes = tabela(fonte, 'Cnaes.zip')
        os.makedirs(os.path.dirname(os.path.abspath(arq_cnaes)), exist_ok=True)
        with open(arq_cnaes, 'w', encoding='utf-8') as f:
            json.dump({k.zfill(7): v for k, v in sorted(cnaes.items())}, f, ensure_ascii=False, separators=(',', ':'))
        log(f'{len(cnaes)} CNAEs gravados em {arq_cnaes}')

    # 1) Estabelecimentos ativos na UF
    estab = []
    for i in range(10):
        caminho, apagar = fonte.abrir(f'Estabelecimentos{i}.zip')
        achados = 0
        for linha in linhas_zip(caminho):
            if not any(m in linha for m in marcas):  # filtro rápido
                continue
            c = campos(linha)
            if len(c) < 21 or c[19].strip() not in UFS or c[5].strip() not in ('02', '2'):
                continue
            secundarios = ','.join(x.strip() for x in c[12].split(',') if x.strip().isdigit())
            estab.append((c[0].strip(), c[0].strip() + c[1].strip() + c[2].strip(),
                          'M' if c[3].strip() == '1' else 'F', normalizar(c[4]),
                          normalizar(municipios.get(c[20].strip(), c[20].strip())),
                          c[11].strip(), secundarios))
            achados += 1
        log(f'  Estabelecimentos{i}: {achados} ativos em {",".join(UFS)}')
        if apagar:
            os.remove(caminho)
    basicos = {e[0] for e in estab}
    log(f'{len(estab)} estabelecimentos ativos, {len(basicos)} empresas')

    # 2) Simples Nacional e MEI (o MEI fica de fora para o índice caber na hospedagem)
    mei = set()
    simples = set()
    caminho, apagar = fonte.abrir('Simples.zip')
    for linha in linhas_zip(caminho):
        b = basico_da_linha(linha)
        if b not in basicos:
            continue
        c = campos(linha)
        if len(c) > 1 and c[1].strip().upper() == 'S':
            simples.add(b)
        if SEM_MEI and len(c) > 4 and c[4].strip().upper() == 'S':
            mei.add(b)
    if apagar:
        os.remove(caminho)
    log(f'{len(simples)} no Simples, {len(mei)} MEI ignorados')

    # 3) Razão social
    razoes = {}
    for i in range(10):
        caminho, apagar = fonte.abrir(f'Empresas{i}.zip')
        for linha in linhas_zip(caminho):
            b = basico_da_linha(linha)
            if b not in basicos or b in mei:
                continue
            c = campos(linha)
            razoes[b] = normalizar(c[1])
        if apagar:
            os.remove(caminho)

    linhas = []
    for basico, cnpj, matriz, fantasia, municipio, cnae, secundarios in estab:
        if basico in mei:
            continue
        razao = razoes.get(basico, '')
        if not razao and not fantasia:
            continue
        linhas.append(f'{cnpj}|{razao}|{fantasia if fantasia != razao else ""}|{municipio}|{matriz}|{cnae}|{secundarios}|'
                      + ('S' if basico in simples else 'N'))
    linhas.sort(key=lambda l: l.split('|', 2)[1])
    return linhas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--saida', required=True, help='arquivo .txt.gz')
    ap.add_argument('--info', required=True, help='arquivo .json com referência e tamanho')
    ap.add_argument('--cnaes', help='arquivo .json da tabela de CNAEs (código → descrição)')
    ap.add_argument('--local', help='pasta com os ZIPs (teste sem baixar)')
    ap.add_argument('--mes', help='YYYY-MM (padrão: último mês completo)')
    ap.add_argument('--forcar', action='store_true')
    a = ap.parse_args()

    mes = a.mes or ('local' if a.local else mes_mais_recente())
    log(f'Base de referência: {mes}')
    if not a.forcar and not a.local and os.path.exists(a.info):
        try:
            with open(a.info, encoding='utf-8') as f:
                atual = json.load(f)
            if atual.get('referencia') == mes and atual.get('uf') == ','.join(UFS) and atual.get('sem_mei') == SEM_MEI \
                    and atual.get('versao') == VERSAO:
                log('Este mês já foi processado. Nada a fazer.')
                print('NADA_NOVO')
                return
        except Exception:
            pass

    linhas = processar(Fonte(mes, a.local, tmp='tmp_cnpj_nomes'), a.cnaes)
    for p in (a.saida, a.info):
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with gzip.open(a.saida, 'wt', encoding='ascii', compresslevel=9) as f:
        for l in linhas:
            f.write(l + '\n')
    info = {
        'gerado_em': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'referencia': mes,
        'uf': ','.join(UFS),
        'sem_mei': SEM_MEI,
        'total': len(linhas),
        'bytes_gz': os.path.getsize(a.saida),
        'bytes_texto': sum(len(l) + 1 for l in linhas),
        'versao': VERSAO,
        'formato': 'CNPJ|RAZAO|FANTASIA|MUNICIPIO|M/F|CNAE|CNAES_SECUNDARIOS|SIMPLES',
    }
    with open(a.info, 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    log(f'Gravado {a.saida}: {len(linhas)} empresas, {info["bytes_gz"] / 1e6:.1f} MB compactado')


if __name__ == '__main__':
    main()
