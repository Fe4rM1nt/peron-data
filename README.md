# Peron — automatyczne dane kolejowe

Codzienny importer dla [Peronu](https://peron-kolej-live.fabledbench32.chatgpt.site).

- **Linie**: [PolishTrainsGTFS](https://mkuran.pl/gtfs/), publiczny rozkład GTFS. Dopasowanie po numerze pociągu, przewoźniku i dacie kursowania.
- **Kierunki w Halinowie**: z kolejności przystanków w `stop_times.txt` i długości geograficznej następnej stacji po Halinowie. Brak ręcznej listy stacji końcowych. Niepewne przypadki pozostają nieustalone.
- **Kierunki na pozostałych stacjach**: zapis kolejnych stacji dla każdego potwierdzonego kursu KM/SKM. Peron grupuje odjazdy według najbliższej stacji na rzeczywistej trasie, a nie nazwy stacji końcowej.
- **Katalog stacji**: wyłącznie rzeczywiste przystanki z tras KM i SKM Warszawa w GTFS, identyfikowane tymi samymi kodami co rozkład. Nie wykorzystujemy błędnie dopasowanych kodów Bilkom/UIC.
- **Tablice awaryjne**: `data/boards/<stop-id>.json` z planowymi odjazdami na kolejne 4 dni dla każdej stacji KM i SKM. Nie zawierają zmyślonych opóźnień.
- **Planowany tabor KM**: [oficjalne zestawienia](https://www.mazowieckie.com.pl/pl/rozklad-jazdy). Odczyt numeru pociągu, zestawienia, liczby jednostek i dni obowiązywania.
- **Odjazdy/opóźnienia na żywo**: pobierane oddzielnie przez Peron z Bilkom / KalkulatorKolejowy.pl.

GitHub Actions uruchamia `scripts/update_data.py` codziennie o 03:17 UTC oraz po zmianach importera. Wyniki: publiczne `data/peron.json`, `data/stations.json` i tablice w `data/boards/` (bez danych prywatnych). Źródłem jest publiczny rozkład PKP PLK/KM w opracowaniu [MKuranowski/PolishTrainsGTFS](https://mkuran.pl/gtfs/).

Nieznane lub konfliktujące przypisania są pomijane. Ten plik podaje tabor **planowany**, a nie gwarantowany. Jeśli źródło jest niedostępne lub nowa publikacja nie zostanie poprawnie rozpoznana, aktualizacja kończy się błędem i zachowuje ostatni poprawny plik; Peron powinien pokazywać znacznik wieku danych.

Do uruchomienia lokalnie: `pip install -r requirements.txt`, następnie `python scripts/update_data.py`.
