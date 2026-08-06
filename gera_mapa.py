# -*- coding: utf-8 -*-
"""Gera docs/dados.json (+ docs/index.html): mapa dos reservatórios (NE + SIN).

Consulta o data lake da ANA e produz um HTML estático interativo (Leaflet)
que busca os dados via fetch em tempo de execução — não são mais embutidos
no HTML. Isso separa duas coisas que antes ficavam acopladas:

  - docs/index.html: o "app" (mapa, filtros, exportação). Só muda quando o
    template.html do projeto é editado — não a cada execução diária.
  - docs/dados.json: o dado do dia. O template.html tenta buscá-lo primeiro
    via raw.githubusercontent.com (serve o conteúdo direto do Git, sem
    passar pelo pipeline de build do GitHub Pages) e só cai para o caminho
    relativo (servido pelo Pages) se isso falhar.

Motivo: em 06/08/2026 o GitHub Pages ficou com o *build* travado por horas
(incidente githubstatus.com) enquanto o `git push` continuava funcionando
normalmente. Com os dados vindo do Git direto, a atualização diária deixa
de depender do Pages "publicar" — só precisa do push ter dado certo.

Rodar com o python do venv do projeto "app bancos ANA" (tem pyodbc/pandas
e a auth Entra ID em cache). Em caso de falha, preserva a saída anterior
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

RAIZ = Path(__file__).resolve().parent
APP_BANCOS = r"C:\Users\diego\Projects\claude-code\app bancos ANA"
sys.path.insert(0, APP_BANCOS)
sys.path.insert(0, str(RAIZ / "vendor"))  # pyshp vendorizado (shapefile.py)

logging.basicConfig(
    filename=RAIZ / "logs" / "gera_mapa.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# Fonte: dbo.medicaodado (última medição por reservatório) + cadastro em
# dbo.caract_reservatorio. NÃO usar dbo.vw_reservatoriopnt: ela fica ~1 dia
# atrás da medicaodado (constatado em 31/07/2026 — SIN 29/07 na view vs 30/07
# na tabela). Regras: datas futuras descartadas (erro na fonte); NE/UHE exigem
# o valor relevante preenchido (volume/VU) — fio d'água entra mesmo sem cota
# (ex.: Teles Pires não reporta cota).
SQL = """
WITH cad AS (
    SELECT c.*, ROW_NUMBER() OVER (PARTITION BY c.res_id ORDER BY c.mun_cod_ibge) AS rn_cad
    FROM dbo.caract_reservatorio c
),
base AS (
    SELECT
        c.tsi_nome, c.tre_nome, c.res_nome, c.est_sigla, c.rio_nome, c.bac_nome,
        c.res_capacidade,
        TRY_CAST(REPLACE(c.res_latitude, ',', '.') AS float) AS lat,
        TRY_CAST(REPLACE(c.res_longitude, ',', '.') AS float) AS lon,
        m.med_data_medicao, m.med_volume, m.med_vol_util, m.med_cota,
        ROW_NUMBER() OVER (PARTITION BY m.med_res_id ORDER BY m.med_data_medicao DESC) AS rn
    FROM dbo.medicaodado m
    JOIN cad c ON c.res_id = m.med_res_id AND c.rn_cad = 1
    WHERE m.med_data_medicao <= GETDATE()
      AND TRY_CAST(REPLACE(c.res_latitude, ',', '.') AS float) IS NOT NULL
      AND TRY_CAST(REPLACE(c.res_longitude, ',', '.') AS float) IS NOT NULL
      AND (c.tsi_nome = 'NORDESTE'
           OR (c.tsi_nome = 'SIN' AND c.tre_nome IN ('Usina com Reservatório', 'Usina a Fio dÁgua')))
      AND (c.tre_nome = 'Usina a Fio dÁgua'
           OR (CASE WHEN c.tsi_nome = 'NORDESTE' THEN m.med_volume
                    ELSE m.med_vol_util END) IS NOT NULL)
)
SELECT
    CASE WHEN tsi_nome = 'NORDESTE' THEN 'Nordeste'
         WHEN tre_nome = 'Usina a Fio dÁgua' THEN 'SIN - Fio d''água'
         ELSE 'SIN - Reservatório' END AS grupo,
    LTRIM(RTRIM(res_nome))  AS nome,
    LTRIM(RTRIM(tre_nome))  AS tipo,
    LTRIM(RTRIM(est_sigla)) AS uf,
    LTRIM(RTRIM(rio_nome))  AS rio,
    LTRIM(RTRIM(bac_nome))  AS bacia,
    lat, lon,
    ROUND(CASE WHEN tsi_nome = 'NORDESTE' THEN med_volume / NULLIF(res_capacidade, 0) * 100
               WHEN tre_nome = 'Usina a Fio dÁgua' THEN NULL
               ELSE med_vol_util END, 1) AS pct,
    CASE WHEN tre_nome = 'Usina a Fio dÁgua' THEN 'Fio d''água'
         WHEN (CASE WHEN tsi_nome = 'NORDESTE' THEN med_volume / NULLIF(res_capacidade, 0) * 100
                    ELSE med_vol_util END) IS NULL THEN 'Sem dado'
         WHEN (CASE WHEN tsi_nome = 'NORDESTE' THEN med_volume / NULLIF(res_capacidade, 0) * 100
                    ELSE med_vol_util END) < 20 THEN 'Restrição'
         WHEN (CASE WHEN tsi_nome = 'NORDESTE' THEN med_volume / NULLIF(res_capacidade, 0) * 100
                    ELSE med_vol_util END) <= 50 THEN 'Atenção'
         ELSE 'Normal' END AS faixa,
    ROUND(med_cota, 2) AS cota,
    CONVERT(varchar(10), med_data_medicao, 103) AS data_med,
    DATEDIFF(day, med_data_medicao, GETDATE()) AS dias
FROM base
WHERE rn = 1
"""


def consultar():
    from ana_datalake import connect, read_sql

    conn = connect("reservatorio", interactive=False)
    df = read_sql(SQL, conn)
    if len(df) < 500:  # sanidade: hoje são ~706; menos que isso indica problema na fonte
        raise RuntimeError(f"consulta retornou só {len(df)} linhas — abortando por seguranca")
    registros = df.to_dict(orient="records")
    # json.dumps emite NaN como literal `NaN` — inválido em JSON estrito.
    # Isso não dava problema quando os dados eram embutidos direto no <script>
    # (NaN é um identificador JS válido ali), mas agora que o navegador faz
    # JSON.parse() de verdade num fetch, um único NaN quebra o parse inteiro.
    # `v != v` é o teste clássico de NaN (é o único valor que não é igual a
    # si mesmo) — mais simples que importar math só para isso.
    for r in registros:
        for k, v in r.items():
            if isinstance(v, float) and v != v:
                r[k] = None
    return registros


def _escreve_atomico(destino, conteudo):
    tmp = destino.with_suffix(".tmp")
    tmp.write_text(conteudo, encoding="utf-8")
    tmp.replace(destino)  # troca atômica: nunca deixa o arquivo pela metade


def gerar_saida(registros):
    payload = {
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "registros": registros,
    }
    dados_json = RAIZ / "docs" / "dados.json"
    _escreve_atomico(dados_json, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    # index.html não leva mais os dados embutidos — é cópia direta do
    # template (muda só quando o template.html é editado, não a cada rodada).
    index_html = RAIZ / "docs" / "index.html"
    _escreve_atomico(index_html, (RAIZ / "template.html").read_text(encoding="utf-8"))
    return dados_json, index_html


# Cores das faixas em formato KML (aabbggrr) e ícones por grupo
KML_CORES = {
    "Restrição": "ff0000c0",   # #C00000
    "Atenção": "ff0fc8f2",     # #F2C80F
    "Normal": "ff107c10",      # #107C10
    "Fio d'água": "ffa3a095",  # #95A0A3 (cinza claro p/ não confundir com o verde)
    "Sem dado": "ff9e9e9e",
}
# Mesmas cores em #RRGGBB (p/ swatches HTML da legenda — KML_CORES está em
# aabbggrr, ordem que o CSS não entende).
FAIXA_HEX = {
    "Restrição": "#C00000",
    "Atenção": "#F2C80F",
    "Normal": "#107C10",
    "Fio d'água": "#95A0A3",
    "Sem dado": "#9E9E9E",
}
KML_ICONES = {
    "Nordeste": "http://maps.google.com/mapfiles/kml/shapes/placemark_square.png",
    "SIN - Reservatório": "http://maps.google.com/mapfiles/kml/shapes/triangle.png",
    "SIN - Fio d'água": "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png",
}
# Nível 1 da pasta = Classificação (faixa); nível 2 = Grupo. "Fio d'água" só
# tem o grupo "SIN - Fio d'água" dentro — as demais faixas têm Nordeste e
# SIN - Reservatório. Ordem pedida pelo usuário: Fio d'água primeiro.
FAIXAS_ORDEM = ["Fio d'água", "Restrição", "Atenção", "Normal", "Sem dado"]
FAIXAS_ROTULO = {
    "Restrição": "Restrição (<20%)",
    "Atenção": "Atenção (20–50%)",
    "Normal": "Normal (>50%)",
    "Fio d'água": "Fio d'água (sem classificação)",
    "Sem dado": "Sem dado",
}
GRUPOS_ORDEM = ["SIN - Fio d'água", "Nordeste", "SIN - Reservatório"]


def _legenda_html():
    swatches = "".join(
        f"<div style='margin:2px 0'><span style='display:inline-block;width:12px;height:12px;"
        f"background:{FAIXA_HEX[f]};border:1px solid #888;margin-right:6px;'></span>{escape(FAIXAS_ROTULO[f])}</div>"
        for f in ("Restrição", "Atenção", "Normal", "Fio d'água")
    )
    return (
        "<b>Cor — Classificação</b><br/>" + swatches +
        "<br/><b>Forma — Grupo</b><br/>"
        "Quadrado: Nordeste — Volume (%)<br/>"
        "Triângulo: SIN – UHE c/ reservatório — Volume Útil (%)<br/>"
        "Círculo: SIN – UHE a fio d'água — Nível (m)"
    )


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
    # corpo[faixa][grupo] = lista de placemarks — pastas aninhadas em 2 níveis
    corpo = {}
    for r in registros:
        faixa, grupo = r["faixa"], r["grupo"]
        chave = (grupo, faixa)
        sid = f"s_{abs(hash(chave))}"
        if chave not in usados:
            usados.add(chave)
            estilos.append(
                f"<Style id='{sid}'><IconStyle><color>{KML_CORES.get(faixa, KML_CORES['Sem dado'])}</color>"
                f"<scale>0.9</scale><Icon><href>{KML_ICONES[grupo]}</href></Icon></IconStyle>"
                f"<LabelStyle><scale>0</scale></LabelStyle></Style>"
            )
        valor, _, rotulo = _valor_unidade(r)
        casas = 2 if grupo == "SIN - Fio d'água" else 1
        desc = (
            f"<b>{rotulo}:</b> {_fmt_br(valor, casas)}<br/>"
            f"<b>Estado:</b> {escape(r.get('uf') or '–')}<br/>"
            f"<b>Data do Dado:</b> {escape(r.get('data_med') or '–')}"
        )
        placemark = (
            f"<Placemark><name>{escape(r['nome'])}</name><styleUrl>#{sid}</styleUrl>"
            f"<description><![CDATA[{desc}]]></description>"
            f"<Point><coordinates>{r['lon']},{r['lat']},0</coordinates></Point></Placemark>"
        )
        corpo.setdefault(faixa, {}).setdefault(grupo, []).append(placemark)

    pastas_faixa = []
    for faixa in FAIXAS_ORDEM:
        grupos = corpo.get(faixa)
        if not grupos:
            continue
        subpastas = "".join(
            f"<Folder><name>{escape(g)}</name>{''.join(grupos[g])}</Folder>"
            for g in GRUPOS_ORDEM if grupos.get(g)
        )
        pastas_faixa.append(f"<Folder><name>{escape(FAIXAS_ROTULO.get(faixa, faixa))}</name>{subpastas}</Folder>")

    legenda = _legenda_html()
    pasta_legenda = f"<Folder><name>Legenda</name><description><![CDATA[{legenda}]]></description></Folder>"
    kml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<kml xmlns='http://www.opengis.net/kml/2.2'><Document>"
        f"<name>Situação dos Reservatórios — Nordeste e SIN</name>"
        f"<description><![CDATA[{legenda}]]></description>"
        f"{''.join(estilos)}{pasta_legenda}{''.join(pastas_faixa)}"
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
        dados_json, index_html = gerar_saida(registros)
        gerar_kmz(registros)
        gerar_shapefile(registros)
        logging.info("ok: %d reservatorios -> %s (+%s +kmz +shp)", len(registros), dados_json, index_html)
        print(f"OK: {len(registros)} reservatorios (json + html + kmz + shp)")
        return 0
    except Exception:
        logging.exception("falha na geracao do mapa")
        print("ERRO: ver logs/gera_mapa.log", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
