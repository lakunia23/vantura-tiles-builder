# Vantura - budowa kafli map

Darmowa budowa kafli (graf dróg GraphHopper, bogate opisy atrakcji po polsku, kolorowe szlaki) w GitHub Actions.
Gotowe kafle są w zakładce Releases (wydanie = obszar 10x10 stopni, np. kafle-N50_E010).
Dane: © OpenStreetMap contributors (ODbL), opisy: Wikipedia (CC BY-SA), Wikidata (CC0).
Tłumaczenia obcojęzycznej Wikipedii: TranslateGemma 4B (Ollama) — każdy punkt, bez rang, ze wznawianą pamięcią tłumaczeń.

- Workflow **0. Usuń wszystkie kafle** - przed budową od nowa (wpisz USUN).
- Workflow **1. Zbuduj kafle** - uruchamiany ręcznie (world = cały świat); kolejne partie startują same.

Sekret repozytorium: WIKI_CONTACT. Zmienna repozytorium (opcjonalna): TLUMACZENIA_AI (domyślnie włączone; 0 = wyłączone).
