# -*- coding: utf-8 -*-
"""Gera docs/index.html: mapa da situação dos reservatórios (Nordeste + SIN).

Consulta o data lake da ANA (view dbo.vw_reservatoriopnt) e produz um HTML
estático interativo (Leaflet). Forma do marcador = grupo; cor = faixa.
Rodar com o python do venv do projeto "app bancos ANA" (tem pyodbc/pandas
e a auth Entra ID em cache). Em caso de falha, preserva o HTML anterior
e sai com código != 0 para o publicador não commitar nada.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from string import Template

RAIZ = Path(__file__).resolve().parent
APP_BANCOS = r"C:\Users\diego\Projects\claude-code\app bancos ANA"
sys.path.insert(0, APP_BANCOS)

logging.basicConfig(
    filename=RAIZ / "logs" / "gera_mapa.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

SQL = """
SELECT
    CASE
        WHEN SAR_TIPO_SISTEMA = 'NORDESTE' THEN 'Nordeste'
        WHEN SAR_TIPO_RESERVATORIO = 'Usina a Fio dÁgua' THEN 'SIN - Fio d''água'
        ELSE 'SIN - Reservatório'
    END AS grupo,
    LTRIM(RTRIM(SAR_RES_NOME))          AS nome,
    LTRIM(RTRIM(SAR_TIPO_RESERVATORIO)) AS tipo,
    LTRIM(RTRIM(SAR_SG_UF))             AS uf,
    LTRIM(RTRIM(SAR_RIO_NOME))          AS rio,
    LTRIM(RTRIM(SAR_BACIA_NOME))        AS bacia,
    SAR_LATITUDE                        AS lat,
    SAR_LONGITUDE                       AS lon,
    ROUND(CASE
        WHEN SAR_TIPO_SISTEMA = 'NORDESTE' THEN SAR_PC_VOLUME
        WHEN SAR_TIPO_RESERVATORIO = 'Usina a Fio dÁgua' THEN NULL
        ELSE SAR_ULT_VOLUME_UTIL
    END, 1) AS pct,
    CASE
        WHEN SAR_TIPO_RESERVATORIO = 'Usina a Fio dÁgua' THEN 'Fio d''água'
        WHEN (CASE WHEN SAR_TIPO_SISTEMA = 'NORDESTE' THEN SAR_PC_VOLUME
                   ELSE SAR_ULT_VOLUME_UTIL END) IS NULL THEN 'Sem dado'
        WHEN (CASE WHEN SAR_TIPO_SISTEMA = 'NORDESTE' THEN SAR_PC_VOLUME
                   ELSE SAR_ULT_VOLUME_UTIL END) < 20 THEN 'Restrição'
        WHEN (CASE WHEN SAR_TIPO_SISTEMA = 'NORDESTE' THEN SAR_PC_VOLUME
                   ELSE SAR_ULT_VOLUME_UTIL END) <= 50 THEN 'Atenção'
        ELSE 'Normal'
    END AS faixa,
    ROUND(SAR_ULT_COTA, 2)              AS cota,
    ROUND(SAR_ULT_AFLUENCIA, 1)         AS afl,
    ROUND(SAR_ULT_DEFLUENCIA, 1)        AS defl,
    CONVERT(varchar(10), SAR_ULT_DATAMEDICAO, 103) AS data_med,
    DATEDIFF(day, SAR_ULT_DATAMEDICAO, GETDATE()) AS dias
FROM dbo.vw_reservatoriopnt
WHERE (SAR_TIPO_SISTEMA = 'NORDESTE'
       OR (SAR_TIPO_SISTEMA = 'SIN'
           AND SAR_TIPO_RESERVATORIO IN ('Usina com Reservatório', 'Usina a Fio dÁgua')))
  AND SAR_LATITUDE IS NOT NULL AND SAR_LONGITUDE IS NOT NULL
"""


def consultar():
    from ana_datalake import connect, read_sql

    conn = connect("reservatorio", interactive=False)
    df = read_sql(SQL, conn)
    if len(df) < 500:  # sanidade: hoje são ~706; menos que isso indica problema na fonte
        raise RuntimeError(f"consulta retornou só {len(df)} linhas — abortando por seguranca")
    df = df.where(df.notna(), None)
    return df.to_dict(orient="records")


def gerar_html(registros):
    template = Template((RAIZ / "template.html").read_text(encoding="utf-8"))
    html = template.substitute(
        DADOS_JSON=json.dumps(registros, ensure_ascii=False, separators=(",", ":")),
        GERADO_EM=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )
    destino = RAIZ / "docs" / "index.html"
    tmp = destino.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(destino)  # troca atômica: nunca deixa index.html pela metade
    return destino


def main():
    try:
        registros = consultar()
        destino = gerar_html(registros)
        logging.info("ok: %d reservatorios -> %s", len(registros), destino)
        print(f"OK: {len(registros)} reservatorios")
        return 0
    except Exception:
        logging.exception("falha na geracao do mapa")
        print("ERRO: ver logs/gera_mapa.log", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
