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
from io import BytesIO
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


# Cores das faixas em #RRGGBB — mesmas do mapa web (template.html: CORES) e
# da legenda em HTML/PNG.
FAIXA_HEX = {
    "Restrição": "#C00000",
    "Atenção": "#F2C80F",
    "Normal": "#107C10",
    "Fio d'água": "#95A0A3",
    "Sem dado": "#9E9E9E",
}
# Forma do marcador por grupo — igual ao mapa web (svgMarcador) e à legenda.
GRUPO_FORMA = {
    "Nordeste": "quadrado",
    "SIN - Reservatório": "triangulo",
    "SIN - Fio d'água": "circulo",
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


def _hex_rgba(hexcor, alpha=255):
    hexcor = hexcor.lstrip("#")
    return tuple(int(hexcor[i:i + 2], 16) for i in (0, 2, 4)) + (alpha,)


def gerar_icone_png(tipo, cor_hex, tamanho=32):
    """Ícone do marcador (quadrado/triângulo/círculo, cor sólida) — mesma
    forma/estilo do mapa web e da legenda, em vez do ícone genérico do Google
    (que fica com o multiply de cor meio embaçado e não bate com a legenda).
    Desenha em 4x e reduz com LANCZOS: ImageDraw não faz antialiasing nativo,
    então polígonos sem esse truque saem serrilhados.
    """
    from PIL import Image, ImageDraw

    grande = tamanho * 4
    img = Image.new("RGBA", (grande, grande), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cor = _hex_rgba(cor_hex)
    branco = (255, 255, 255, 230)
    margem = grande * 0.12
    contorno = max(1, round(grande * 0.035))
    if tipo == "quadrado":
        d.rectangle([margem, margem, grande - margem, grande - margem], fill=cor, outline=branco, width=contorno)
    elif tipo == "triangulo":
        d.polygon(
            [(grande / 2, margem), (grande - margem, grande - margem), (margem, grande - margem)],
            fill=cor, outline=branco, width=contorno,
        )
    else:
        d.ellipse([margem, margem, grande - margem, grande - margem], fill=cor, outline=branco, width=contorno)
    img = img.resize((tamanho, tamanho), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def gerar_legenda_png():
    """Legenda como imagem (PNG) p/ <ScreenOverlay> do KML — ao contrário da
    descrição de uma pasta (só aparece em balão, ao clicar), o ScreenOverlay
    fica fixo num canto da tela, sempre visível, sem precisar clicar em nada.
    """
    from PIL import Image, ImageDraw, ImageFont

    # Fonte padrão do Pillow (bitmap) não desenha acentos — "ç"/"ã"/"í" viram
    # caixas. Usa Segoe UI do Windows (mesma família do resto do mapa); se um
    # dia isto rodar fora deste notebook, cai pro Arial e por fim pro bitmap.
    def _fonte(candidatos, tamanho):
        for caminho in candidatos:
            if Path(caminho).exists():
                return ImageFont.truetype(caminho, tamanho)
        return ImageFont.load_default(size=tamanho)

    e = 2  # escala 2x p/ ficar nítido em telas de alta densidade
    largura, altura = 250 * e, 190 * e
    img = Image.new("RGBA", (largura, altura), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [1, 1, largura - 2, altura - 2], radius=8 * e,
        fill=(255, 255, 255, 242), outline=(153, 153, 153, 255), width=e,
    )
    f_titulo = _fonte([r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\arialbd.ttf"], 13 * e)
    f_texto = _fonte([r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"], 12 * e)
    preto = (17, 17, 17, 255)
    cinza = (120, 120, 120, 255)

    def forma(cx, cy, tipo):
        r = 6 * e
        if tipo == "quadrado":
            d.rectangle([cx - r, cy - r, cx + r, cy + r], fill=cinza, outline=(255, 255, 255, 255), width=e // 2 or 1)
        elif tipo == "triangulo":
            d.polygon([(cx, cy - r), (cx + r, cy + r), (cx - r, cy + r)], fill=cinza, outline=(255, 255, 255, 255))
        else:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=cinza, outline=(255, 255, 255, 255), width=e // 2 or 1)

    y = 16 * e
    d.text((12 * e, y), "Cor — Classificação", font=f_titulo, fill=preto)
    for faixa in ("Restrição", "Atenção", "Normal", "Fio d'água"):
        y += 18 * e
        d.rectangle([12 * e, y - 5 * e, 24 * e, y + 7 * e], fill=_hex_rgba(FAIXA_HEX[faixa]), outline=(136, 136, 136, 255))
        d.text((30 * e, y - 5 * e), FAIXAS_ROTULO[faixa], font=f_texto, fill=preto)
    y += 24 * e
    d.text((12 * e, y), "Forma — Grupo", font=f_titulo, fill=preto)
    for rotulo, tipo in (
        ("Nordeste — Volume (%)", "quadrado"),
        ("SIN – UHE c/ reserv. — Volume Útil (%)", "triangulo"),
        ("SIN – UHE a fio d'água — Nível (m)", "circulo"),
    ):
        y += 18 * e
        forma(18 * e, y + 1 * e, tipo)
        d.text((30 * e, y - 5 * e), rotulo, font=f_texto, fill=preto)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


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
    icones = {}  # nome_arquivo -> bytes PNG
    # corpo[faixa][grupo] = lista de placemarks — pastas aninhadas em 2 níveis
    corpo = {}
    for r in registros:
        faixa, grupo = r["faixa"], r["grupo"]
        chave = (grupo, faixa)
        sid = f"s_{abs(hash(chave))}"
        if chave not in usados:
            usados.add(chave)
            nome_icone = f"icone_{sid}.png"
            icones[nome_icone] = gerar_icone_png(GRUPO_FORMA[grupo], FAIXA_HEX.get(faixa, FAIXA_HEX["Sem dado"]))
            estilos.append(
                f"<Style id='{sid}'><IconStyle><scale>1.0</scale><Icon><href>{nome_icone}</href></Icon>"
                f"<hotSpot x='0.5' y='0.5' xunits='fraction' yunits='fraction'/></IconStyle>"
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
    # ScreenOverlay: imagem fixa no canto inferior esquerdo da tela, sempre
    # visível (diferente de uma pasta com <description>, que só aparece em
    # balão ao clicar). overlayXY 0,0 = canto inferior-esquerdo da IMAGEM;
    # screenXY 0.01,0.02 = quase colado no canto inferior-esquerdo da TELA.
    # size em pixels = metade do PNG (gerado a 2x/250x190) — exibido a 125x95,
    # mas com o dobro dos pixels de origem por trás, fica nítido (efeito
    # "retina"), não borrado como encolher a própria imagem deixaria.
    overlay_legenda = (
        "<ScreenOverlay><name>Legenda</name>"
        "<Icon><href>legenda.png</href></Icon>"
        "<overlayXY x='0' y='0' xunits='fraction' yunits='fraction'/>"
        "<screenXY x='0.01' y='0.02' xunits='fraction' yunits='fraction'/>"
        "<size x='125' y='95' xunits='pixels' yunits='pixels'/>"
        "</ScreenOverlay>"
    )
    kml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<kml xmlns='http://www.opengis.net/kml/2.2'><Document>"
        f"<name>Situação dos Reservatórios — Nordeste e SIN</name>"
        f"<description><![CDATA[{legenda}]]></description>"
        f"{''.join(estilos)}{overlay_legenda}{''.join(pastas_faixa)}"
        "</Document></kml>"
    )
    destino = RAIZ / "docs" / "reservatorios.kmz"
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
        z.writestr("legenda.png", gerar_legenda_png())
        for nome, dados in icones.items():
            z.writestr(nome, dados)
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
