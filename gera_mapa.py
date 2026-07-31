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
import tempfile
import zipfile
from datetime import datetime
from html import escape
from pathlib import Path
from string import Template

RAIZ = Path(__file__).resolve().parent
APP_BANCOS = r"C:\Users\diego\Projects\claude-code\app bancos ANA"
sys.path.insert(0, APP_BANCOS)
sys.path.insert(0, str(RAIZ / "vendor"))  # pyshp vendorizado (shapefile.py)

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


# Cores das faixas em formato KML (aabbggrr) e ícones por grupo
KML_CORES = {
    "Restrição": "ff0000c0",   # #C00000
    "Atenção": "ff0fc8f2",     # #F2C80F
    "Normal": "ff107c10",      # #107C10
    "Fio d'água": "ff6d6b5f",  # #5F6B6D
    "Sem dado": "ff9e9e9e",
}
KML_ICONES = {
    "Nordeste": "http://maps.google.com/mapfiles/kml/shapes/placemark_square.png",
    "SIN - Reservatório": "http://maps.google.com/mapfiles/kml/shapes/triangle.png",
    "SIN - Fio d'água": "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png",
}


def _valor_unidade(reg):
    """Valor exibido e sua unidade, conforme o grupo (mesma lógica do popup)."""
    if reg["grupo"] == "SIN - Fio d'água":
        return reg.get("cota"), "m", "Nível (m)"
    rotulo = "Volume (%)" if reg["grupo"] == "Nordeste" else "Volume Útil (%)"
    return reg.get("pct"), "%", rotulo


def _fmt_br(v, casas):
    if v is None:
        return "–"
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def gerar_kmz(registros):
    estilos, usados = [], set()
    corpo = {g: [] for g in KML_ICONES}
    for r in registros:
        faixa = r["faixa"]
        chave = (r["grupo"], faixa)
        sid = f"s_{abs(hash(chave))}"
        if chave not in usados:
            usados.add(chave)
            estilos.append(
                f"<Style id='{sid}'><IconStyle><color>{KML_CORES.get(faixa, KML_CORES['Sem dado'])}</color>"
                f"<scale>0.9</scale><Icon><href>{KML_ICONES[r['grupo']]}</href></Icon></IconStyle>"
                f"<LabelStyle><scale>0</scale></LabelStyle></Style>"
            )
        valor, _, rotulo = _valor_unidade(r)
        casas = 2 if r["grupo"] == "SIN - Fio d'água" else 1
        desc = (
            f"<b>{rotulo}:</b> {_fmt_br(valor, casas)}<br/>"
            f"<b>Estado:</b> {escape(r.get('uf') or '–')}<br/>"
            f"<b>Data do Dado:</b> {escape(r.get('data_med') or '–')}"
        )
        corpo[r["grupo"]].append(
            f"<Placemark><name>{escape(r['nome'])}</name><styleUrl>#{sid}</styleUrl>"
            f"<description><![CDATA[{desc}]]></description>"
            f"<Point><coordinates>{r['lon']},{r['lat']},0</coordinates></Point></Placemark>"
        )
    pastas = "".join(
        f"<Folder><name>{escape(g)}</name>{''.join(pl)}</Folder>" for g, pl in corpo.items() if pl
    )
    kml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<kml xmlns='http://www.opengis.net/kml/2.2'><Document>"
        f"<name>Situação dos Reservatórios — Nordeste e SIN</name>{''.join(estilos)}{pastas}"
        "</Document></kml>"
    )
    destino = RAIZ / "docs" / "reservatorios.kmz"
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
    return destino


PRJ_WGS84 = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,'
    '298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)


def gerar_shapefile(registros):
    import shapefile  # pyshp (vendor/)

    destino = RAIZ / "docs" / "reservatorios_shp.zip"
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "reservatorios"
        with shapefile.Writer(str(base), shapeType=shapefile.POINT, encoding="utf-8") as w:
            w.field("nome", "C", size=100)
            w.field("grupo", "C", size=20)
            w.field("faixa", "C", size=12)
            w.field("valor", "N", decimal=2)
            w.field("unidade", "C", size=4)
            w.field("uf", "C", size=2)
            w.field("data_dado", "C", size=10)
            for r in registros:
                valor, unidade, _ = _valor_unidade(r)
                w.point(r["lon"], r["lat"])
                w.record(r["nome"], r["grupo"], r["faixa"], valor, unidade,
                         r.get("uf") or "", r.get("data_med") or "")
        (base.with_suffix(".prj")).write_text(PRJ_WGS84, encoding="ascii")
        (base.with_suffix(".cpg")).write_text("UTF-8", encoding="ascii")
        with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
            for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                z.write(base.with_suffix(ext), f"reservatorios{ext}")
        # sanidade: reabre o shapefile de dentro do zip recém-gravado
        with shapefile.Reader(str(base)) as rd:
            if len(rd) != len(registros):
                raise RuntimeError(f"shapefile com {len(rd)} registros != {len(registros)}")
    return destino


def main():
    try:
        registros = consultar()
        destino = gerar_html(registros)
        gerar_kmz(registros)
        gerar_shapefile(registros)
        logging.info("ok: %d reservatorios -> %s (+kmz +shp)", len(registros), destino)
        print(f"OK: {len(registros)} reservatorios (html + kmz + shp)")
        return 0
    except Exception:
        logging.exception("falha na geracao do mapa")
        print("ERRO: ver logs/gera_mapa.log", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
