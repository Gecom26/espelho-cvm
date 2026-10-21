#!/usr/bin/env python3
"""
Novas corretoras de seguros no RS — filtro da base aberta de CNPJ da Receita Federal.

Baixa a base mensal (WebDAV público da Receita), separa os estabelecimentos com
CNAE 66.22-3-00 (Corretores e agentes de seguros, de planos de previdência
complementar e de saúde), UF = RS e data de abertura nos últimos N meses, junta
razão social, município e sócios, e grava um JSON leve para a página corretora.php.

Uso:
    python corretoras_rs.py --saida corretoras/corretoras_rs.json
    python corretoras_rs.py --saida saida.json --local pasta_com_zips   (teste, sem baixar)
    python corretoras_rs.py --saida saida.json --forcar                 (reprocessa o mesmo mês)

Variáveis opcionais: MESES (padrão 24), UF (padrão RS), CNAE (padrão 6622300).
"""
import argparse
import base64
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

BASE = 'https://arquivos.receitafederal.gov.br/public.php/webdav/'
TOKEN = 'YggdBLfdninEJX9'  # token público do compartilhamento da Receita (não é segredo)
AUTH = 'Basic ' + base64.b64encode((TOKEN + ':').encode()).decode()

CNAE = os.getenv('CNAE', '6622300')
UF = os.getenv('UF', 'RS')
MESES = int(os.getenv('MESES', '24'))

SITUACAO = {'01': 'NULA', '1': 'NULA', '02': 'ATIVA', '2': 'ATIVA', '03': 'SUSPENSA', '3': 'SUSPENSA',
            '04': 'INAPTA', '4': 'INAPTA', '08': 'BAIXADA', '8': 'BAIXADA'}
PORTE = {'00': '', '01': 'MICRO EMPRESA', '03': 'EMPRESA DE PEQUENO PORTE', '05': 'DEMAIS'}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------- download
def requisicao(url, metodo='GET', cabecalhos=None):
    req = urllib.request.Request(url, method=metodo)
    req.add_header('Authorization', AUTH)
    req.add_header('User-Agent', 'corretoras-rs/1.0 (dados abertos CNPJ)')
    for k, v in (cabecalhos or {}).items():
        req.add_header(k, v)
    return req


def listar(pasta=''):
    """PROPFIND Depth 1 → lista de (nome, tamanho)."""
    req = requisicao(BASE + pasta, 'PROPFIND', {'Depth': '1'})
    with urllib.request.urlopen(req, timeout=120) as r:
        xml = r.read()
    ns = {'d': 'DAV:'}
    itens = []
    for resp in ET.fromstring(xml).findall('d:response', ns):
        href = urllib.request.unquote(resp.findtext('d:href', '', ns)).rstrip('/')
        nome = href.split('/')[-1]
        tam = resp.findtext('.//d:getcontentlength', '0', ns) or '0'
        itens.append((nome, int(tam)))
    return itens


def mes_mais_recente():
    """Último mês (YYYY-MM) com a base completa publicada."""
    meses = sorted({n for n, _ in listar() if re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', n)}, reverse=True)
    for mes in meses:
        nomes = {n for n, _ in listar(mes + '/')}
        if all(f'Estabelecimentos{i}.zip' in nomes for i in range(10)) and 'Municipios.zip' in nomes:
            return mes
        log(f'{mes}: publicação incompleta, tentando o mês anterior')
    raise SystemExit('Nenhum mês completo encontrado na Receita.')


def baixar(mes, nome, destino, tentativas=6):
    """Baixa com retomada (Range) e confere o tamanho."""
    url = f'{BASE}{mes}/{nome}'
    tamanho = None
    for tentativa in range(1, tentativas + 1):
        try:
            feito = os.path.getsize(destino) if os.path.exists(destino) else 0
            cab = {'Range': f'bytes={feito}-'} if feito else {}
            with urllib.request.urlopen(requisicao(url, cabecalhos=cab), timeout=300) as r:
                if feito and r.status != 206:
                    feito = 0  # servidor ignorou o Range: recomeça
                total = r.headers.get('Content-Range', '')
                if '/' in total:
                    tamanho = int(total.split('/')[-1])
                elif r.headers.get('Content-Length'):
                    tamanho = feito + int(r.headers['Content-Length'])
                with open(destino, 'ab' if feito else 'wb') as f:
                    while True:
                        bloco = r.read(4 * 1024 * 1024)
                        if not bloco:
                            break
                        f.write(bloco)
            if tamanho is None or os.path.getsize(destino) == tamanho:
                with open(destino, 'rb') as f:
                    if f.read(2) != b'PK':
                        raise IOError('arquivo não é ZIP')
                return destino
            raise IOError(f'tamanho diferente ({os.path.getsize(destino)} de {tamanho})')
        except (urllib.error.URLError, IOError, TimeoutError, ConnectionError) as e:
            log(f'  {nome}: falha na tentativa {tentativa}: {e}')
            if isinstance(e, urllib.error.HTTPError) and e.code in (404, 416):
                if os.path.exists(destino):
                    os.remove(destino)
            time.sleep(min(60, 10 * tentativa))
    raise SystemExit(f'Não consegui baixar {nome}')


class Fonte:
    """Entrega os ZIPs: baixando da Receita ou lendo de uma pasta local (teste)."""

    def __init__(self, mes, local=None, tmp='tmp_cnpj'):
        self.mes, self.local, self.tmp = mes, local, tmp
        os.makedirs(tmp, exist_ok=True)

    def abrir(self, nome):
        if self.local:
            return os.path.join(self.local, nome), False
        destino = os.path.join(self.tmp, nome)
        log(f'Baixando {nome}...')
        baixar(self.mes, nome, destino)
        return destino, True


def linhas_zip(caminho):
    """Linhas de texto (latin-1) do CSV dentro do ZIP, sem descompactar no disco."""
    with zipfile.ZipFile(caminho) as z:
        for membro in z.namelist():
            with z.open(membro) as bruto:
                for linha in io.TextIOWrapper(bruto, encoding='latin-1', newline=''):
                    yield linha


def campos(linha):
    return next(csv.reader([linha], delimiter=';', quotechar='"'))


def tabela(fonte, nome):
    caminho, apagar = fonte.abrir(nome)
    mapa = {}
    for linha in linhas_zip(caminho):
        c = campos(linha)
        if len(c) >= 2:
            mapa[c[0].strip()] = c[1].strip()
    if apagar:
        os.remove(caminho)
    return mapa


def data_iso(v):
    v = (v or '').strip()
    return f'{v[:4]}-{v[4:6]}-{v[6:8]}' if re.fullmatch(r'\d{8}', v) and v != '00000000' else ''


# ---------------------------------------------------------------- processamento
def processar(fonte, referencia):
    hoje = dt.date.today()
    corte_ano, corte_mes = hoje.year, hoje.month - MESES
    while corte_mes <= 0:
        corte_mes += 12
        corte_ano -= 1
    corte = f'{corte_ano}{corte_mes:02d}01'
    log(f'Filtro: CNAE {CNAE}, UF {UF}, abertura a partir de {corte}')

    municipios = tabela(fonte, 'Municipios.zip')

    # 1) Estabelecimentos
    estab = []
    for i in range(10):
        caminho, apagar = fonte.abrir(f'Estabelecimentos{i}.zip')
        achados = 0
        for linha in linhas_zip(caminho):
            if CNAE not in linha:  # filtro rápido antes de interpretar a linha
                continue
            c = campos(linha)
            if len(c) < 28 or c[19].strip() != UF:
                continue
            principal = c[11].strip() == CNAE
            if not principal and CNAE not in c[12].split(','):
                continue
            if c[10].strip() < corte:
                continue
            estab.append({
                'basico': c[0].strip(),
                'cnpj': c[0].strip() + c[1].strip() + c[2].strip(),
                'matriz': c[3].strip() == '1',
                'fantasia': c[4].strip(),
                'situacao': SITUACAO.get(c[5].strip(), c[5].strip()),
                'data_situacao': data_iso(c[6]),
                'abertura': data_iso(c[10]),
                'cnae_principal': principal,
                'endereco': ' '.join(x for x in [c[13].strip(), c[14].strip()] if x)
                            + (', ' + c[15].strip() if c[15].strip() else '')
                            + (' - ' + c[16].strip() if c[16].strip() else ''),
                'bairro': c[17].strip(),
                'cep': c[18].strip(),
                'municipio': municipios.get(c[20].strip(), c[20].strip()),
                'telefones': [d + t for d, t in ((c[21].strip(), c[22].strip()), (c[23].strip(), c[24].strip())) if t],
                'email': c[27].strip().lower(),
            })
            achados += 1
        log(f'  Estabelecimentos{i}: {achados} corretora(s)')
        if apagar:
            os.remove(caminho)

    basicos = {e['basico'] for e in estab}
    log(f'{len(estab)} estabelecimentos, {len(basicos)} empresas')

    # 2) Empresas (razão social, porte, capital, natureza)
    naturezas = tabela(fonte, 'Naturezas.zip')
    empresas = {}
    for i in range(10):
        caminho, apagar = fonte.abrir(f'Empresas{i}.zip')
        for linha in linhas_zip(caminho):
            b = linha[1:9] if linha.startswith('"') else linha[:8]
            if b not in basicos:
                continue
            c = campos(linha)
            empresas[c[0].strip()] = {
                'razao': c[1].strip(),
                'natureza': naturezas.get(c[2].strip(), c[2].strip()),
                'capital': float((c[4] or '0').replace('.', '').replace(',', '.') or 0),
                'porte': PORTE.get(c[5].strip(), ''),
            }
        if apagar:
            os.remove(caminho)

    # 3) Sócios (nome, qualificação, data de entrada) — sem CPF
    qualificacoes = tabela(fonte, 'Qualificacoes.zip')
    socios = {}
    for i in range(10):
        caminho, apagar = fonte.abrir(f'Socios{i}.zip')
        for linha in linhas_zip(caminho):
            b = linha[1:9] if linha.startswith('"') else linha[:8]
            if b not in basicos:
                continue
            c = campos(linha)
            socios.setdefault(c[0].strip(), []).append({
                'nome': c[2].strip(),
                'qualificacao': qualificacoes.get(c[4].strip(), c[4].strip()),
                'entrada': data_iso(c[5]),
            })
        if apagar:
            os.remove(caminho)

    lista = []
    for e in estab:
        emp = empresas.get(e['basico'], {})
        item = {k: v for k, v in e.items() if k != 'basico'}
        item.update({
            'razao': emp.get('razao', ''),
            'natureza': emp.get('natureza', ''),
            'capital': emp.get('capital', 0),
            'porte': emp.get('porte', ''),
            'socios': socios.get(e['basico'], [])[:15],
        })
        lista.append(item)
    lista.sort(key=lambda x: (x['abertura'], x['razao']), reverse=True)

    return {
        'gerado_em': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'referencia': referencia,
        'cnae': CNAE,
        'uf': UF,
        'meses': MESES,
        'abertura_desde': f'{corte[:4]}-{corte[4:6]}-{corte[6:]}',
        'total': len(lista),
        'corretoras': lista,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--saida', required=True)
    ap.add_argument('--local', help='pasta com os ZIPs (teste sem baixar)')
    ap.add_argument('--mes', help='YYYY-MM (padrão: último mês completo)')
    ap.add_argument('--forcar', action='store_true', help='reprocessa mesmo que o mês já esteja no arquivo')
    a = ap.parse_args()

    mes = a.mes or ('local' if a.local else mes_mais_recente())
    log(f'Base de referência: {mes}')

    if not a.forcar and not a.local and os.path.exists(a.saida):
        try:
            with open(a.saida, encoding='utf-8') as f:
                atual = json.load(f)
            if atual.get('referencia') == mes and atual.get('meses') == MESES:
                log('Este mês já foi processado. Nada a fazer.')
                return
        except Exception:
            pass

    dados = processar(Fonte(mes, a.local), mes)
    os.makedirs(os.path.dirname(os.path.abspath(a.saida)), exist_ok=True)
    with open(a.saida, 'w', encoding='utf-8') as f:
        json.dump(dados, f, ensure_ascii=False, separators=(',', ':'))
    log(f'Gravado {a.saida}: {dados["total"]} corretoras')


if __name__ == '__main__':
    main()
