# Mapa diário — Situação dos Reservatórios (Nordeste e SIN)

Mapa interativo (Leaflet, HTML estático) com a última medição de cada
reservatório, a partir do data lake da ANA (`dbo.vw_reservatoriopnt`):

- **Nordeste** — % do armazenamento total (círculos);
- **SIN reservatórios** — % do volume útil (quadrados);
- **SIN fio d'água** — sem classificação; cota/afluência/defluência (triângulos).

Faixas de cor: **Restrição** <20% · **Atenção** 20–50% · **Normal** >50%.
Marcador esmaecido = medição há mais de 30 dias.

**URL pública:** https://dlpena.github.io/mapa-reservatorios/

## Como funciona

- `gera_mapa.py` — consulta o data lake (venv do projeto "app bancos ANA",
  auth Entra ID em cache) e gera `docs/index.html` a partir de `template.html`.
  Em falha, preserva o HTML anterior e sai com código 1. Grava também
  `docs/reservatorios.kmz`, `docs/reservatorios_shp.zip` e
  `docs/reservatorios.csv` com todos os reservatórios.
- Botões "Baixar" do mapa (KMZ, Shapefile, CSV): geram o arquivo no
  navegador só com os reservatórios visíveis (respeitam os filtros) e, se
  falharem, baixam o arquivo completo de `docs/`. O CSV segue o padrão do
  Excel em português: separador `;`, vírgula decimal, UTF-8 com BOM.
- `publica_mapa.bat` — roda o gerador e faz commit/push do `docs/` (GitHub
  Pages serve essa pasta). Agendado no Task Scheduler (`MapaReservatoriosDiario`,
  diário às 12:00; requer o notebook ligado).

## Rodar manualmente

```bat
publica_mapa.bat
```

Logs em `logs\gera_mapa.log` (geração) e `logs\publica.log` (publicação).
