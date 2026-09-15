#!/usr/bin/env python3
"""
Espelho dos dados da CVM para o sistema de Produção Diária.

Baixa o Informe Diário de Fundos da CVM, mantém só os fundos da grade
(39 CNPJs) e grava um JSON por mês em cotas/inf_diario_AAAAMM.json:

    {"14.159.055/0001-53": {"2026-08-01": 3.214567, ...}, ...}

Roda no GitHub Actions porque o servidor do site não tem rota até a
rede da CVM. Uso local: python espelho_cvm.py 202608 202607
"""

import csv
import io
import json
import os
import sys
import zipfile
from datetime import date
from urllib.request import Request, urlopen

BASE_CVM = "https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{}.zip"
PASTA_SAIDA = "cotas"
MESES_PADRAO = 14  # histórico suficiente para o cálculo de 12 meses

CNPJS = [
    "14.159.055/0001-53", "17.517.380/0001-39", "35.844.987/0001-05",
    "58.686.035/0001-06", "10.583.943/0001-48", "10.586.932/0001-11",
    "21.053.024/0001-89", "31.008.221/0001-30", "21.347.558/0001-18",
    "31.008.331/0001-00", "35.844.983/0001-27", "44.340.104/0001-10",
    "51.800.701/0001-46", "31.008.304/0001-29", "24.022.558/0001-36",
    "06.081.460/0001-78", "21.347.560/0001-97", "28.428.029/0001-98",
    "30.378.546/0001-41", "34.123.581/0001-70", "55.315.314/0001-75",
    "55.239.189/0001-61", "63.594.990/0001-53", "31.008.252/0001-90",
    "31.008.363/0001-05", "21.347.568/0001-53", "34.123.584/0001-04",
    "17.999.997/0001-38", "10.583.948/0001-70", "34.123.602/0001-58",
    "34.123.613/0001-38", "34.123.557/0001-31", "34.123.593/0001-03",
    "34.123.564/0001-33", "45.319.879/0001-77", "45.319.920/0001-05",
    "45.319.963/0001-90", "63.760.470/0001-73", "63.537.250/0001-85",
]


def meses_recentes(quantidade):
    hoje = date.today()
    ano, mes = hoje.year, hoje.month
    saida = []
    for _ in range(quantidade):
        saida.append(f"{ano}{mes:02d}")
        mes -= 1
        if mes == 0:
            mes, ano = 12, ano - 1
    return saida


def baixar(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (espelho-cvm)"})
    with urlopen(req, timeout=180) as resp:
        return resp.read()


def processar_mes(ano_mes, alvo):
    url = BASE_CVM.format(ano_mes)
    print(f"[{ano_mes}] baixando {url}")
    try:
        bruto = baixar(url)
    except Exception as e:
        print(f"[{ano_mes}] falhou: {e}")
        return None

    print(f"[{ano_mes}] {len(bruto) / 1048576:.1f} MB — filtrando")
    cotas = {}
    with zipfile.ZipFile(io.BytesIO(bruto)) as z:
        for nome in z.namelist():
            if not nome.lower().endswith(".csv"):
                continue
            with z.open(nome) as f:
                texto = io.TextIOWrapper(f, encoding="latin-1", newline="")
                leitor = csv.DictReader(texto, delimiter=";")
                col_cnpj = col_data = col_cota = None
                for linha in leitor:
                    if col_cnpj is None:
                        for c in linha.keys():
                            cu = (c or "").upper()
                            if col_cnpj is None and "CNPJ" in cu:
                                col_cnpj = c
                            if col_data is None and "DT_COMPTC" in cu:
                                col_data = c
                            if col_cota is None and "VL_QUOTA" in cu:
                                col_cota = c
                        if not (col_cnpj and col_data and col_cota):
                            print(f"[{ano_mes}] colunas não reconhecidas em {nome}")
                            break
                    cnpj = (linha.get(col_cnpj) or "").strip()
                    if cnpj not in alvo:
                        continue
                    valor = (linha.get(col_cota) or "").strip().replace(",", ".")
                    if not valor:
                        continue
                    cotas.setdefault(cnpj, {})[(linha.get(col_data) or "").strip()] = float(valor)

    print(f"[{ano_mes}] {len(cotas)} fundos encontrados")
    return cotas


def main():
    alvo = set(CNPJS)
    meses = sys.argv[1:] or meses_recentes(MESES_PADRAO)
    os.makedirs(PASTA_SAIDA, exist_ok=True)

    gerados = 0
    for ano_mes in meses:
        caminho = os.path.join(PASTA_SAIDA, f"inf_diario_{ano_mes}.json")
        # Meses fechados não mudam mais: se já existe, não baixa de novo
        if os.path.exists(caminho) and ano_mes not in meses_recentes(2):
            print(f"[{ano_mes}] já existe, pulando")
            continue

        cotas = processar_mes(ano_mes, alvo)
        if not cotas:
            continue
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(cotas, f, ensure_ascii=False, separators=(",", ":"))
        print(f"[{ano_mes}] gravado {caminho} ({os.path.getsize(caminho) / 1024:.0f} KB)")
        gerados += 1

    print(f"\nConcluído — {gerados} arquivo(s) atualizado(s).")
    if gerados == 0:
        print("Nada novo. Se era pra ter dados, confira se a CVM publicou o mês.")


if __name__ == "__main__":
    main()
