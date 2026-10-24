# Momentum Radar — paket ringkas untuk GitHub

Paket ini cukup **9 berkas untuk diunggah ke GitHub**. Seluruh **806 berkas asli**
tetap disertakan di `momentum_radar_bundle.zip`, termasuk kode dashboard, engine
Freqtrade, strategi, dataset historis, pengujian, konfigurasi, dan dokumentasi.
Tidak ada berkas sumber asli yang dihapus atau diubah.

Saat dashboard dijalankan, `bundle_loader.py` memeriksa checksum dan membuka
bundel secara otomatis. Proses ini hanya diperlukan pada awal penggunaan satu
versi bundel. Tidak ada script persiapan tambahan untuk menjalankan dashboard.

## Unggah ke GitHub

1. Ekstrak **ZIP unduhan luar** satu kali.
2. Buka folder `momentum-radar-github` hasil ekstraksi.
3. Di repository GitHub, pilih **Add file → Upload files**.
4. Seret **seluruh isi folder tersebut**, termasuk folder `.streamlit`, ke area
   unggahan. Letakkan isinya langsung di root repository.
5. Klik **Commit changes**.

**Biarkan `momentum_radar_bundle.zip` tetap berupa ZIP.** Itulah satu berkas yang
menampung semua sumber asli; jangan mengekstraknya sebelum unggah. ZIP unduhan
luar sendiri tidak perlu diunggah. GitHub hanya menyimpan ZIP; pembuka otomatis
disediakan oleh `bundle_loader.py` ketika Python/Streamlit menjalankan aplikasi.

Isi repository yang benar:

| Berkas | Kegunaan |
| --- | --- |
| `streamlit_app.py` | Entrypoint aplikasi |
| `bundle_loader.py` | Membuka bundel dan menjalankan aplikasi asli |
| `momentum_radar_bundle.zip` | Semua 806 berkas asli; sekitar 19,75 MiB |
| `requirements.txt` | Dependensi dashboard, sama seperti paket asli |
| `README.md` | Panduan ini |
| `LICENSE` | Lisensi asli |
| `.gitignore` | Mencegah runtime, kredensial lokal, dan database ikut commit lewat Git |
| `.streamlit/config.toml` | Tema dan konfigurasi Streamlit asli |
| `.streamlit/secrets.toml.example` | Contoh konfigurasi koneksi backend |

Batas unggah browser GitHub adalah 100 berkas per unggahan dan 25 MiB per
berkas. Semua berkas paket ini berada di bawah batas tersebut.
Sumber: [GitHub Docs](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository).

## Jalankan di Streamlit Community Cloud

1. Buka [Streamlit Community Cloud](https://share.streamlit.io/) dan buat aplikasi.
2. Pilih repository dan branch yang berisi paket ini.
3. Isi **Main file path** dengan `streamlit_app.py`.
4. Pilih Python **3.12** di pengaturan lanjutan, lalu deploy.

Tanpa URL backend, dashboard menggunakan data historis yang sudah ada dalam
bundel. Untuk menghubungkan bot yang sudah berjalan, isi Streamlit **Secrets**
mengikuti `.streamlit/secrets.toml.example`. File konfigurasi dan Secrets tetap
dibaca dari root repository seperti biasa.

Panduan resmi: [deployment](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
dan [dependensi aplikasi](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies).

## Jalankan di komputer sendiri

Di terminal dalam folder ini:

```bash
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

## Fitur dan backend

Ketujuh halaman tetap menggunakan kode asli: Ringkasan, Market Scanner,
Alert Center, Open Positions, Trade History, Strategy & Risk Settings, serta
System Health & Logs. Strategi, evaluasi historis, klien API, kontrol bot,
konfigurasi dry-run/live, dan pengujian asli juga tetap ada.

Arsitektur aplikasi tetap sama: dashboard terhubung ke backend Freqtrade yang
berjalan terpisah di komputer/VPS. Mengunggah ke GitHub atau membuka dashboard
tidak otomatis menyalakan bot trading. Paket ini mempertahankan fitur yang
sudah ada, bukan menambahkan layanan hosting backend.

Untuk memperoleh kembali proyek lengkap yang bisa diedit atau digunakan
untuk backend, jalankan perintah opsional berikut di komputer sendiri:

```bash
python bundle_loader.py unpack
```

Hasilnya adalah folder `momentum-radar-full` berisi tepat 806 berkas asli.
Panduan backend tetap tersedia di
`momentum-radar-full/docs/FREQTRADE_BACKEND_DEPLOYMENT.md`, beserta dependensi,
Dockerfile, strategi, dan konfigurasi aslinya. Ekspor menolak menimpa folder
yang sudah ada. Untuk memilih lokasi berbeda:

```bash
python bundle_loader.py unpack proyek-lengkap-saya
```

Folder runtime otomatis `.momentum_runtime` dan hasil ekspor lengkap tidak
perlu diunggah lagi ke GitHub. Jika menggunakan unggahan browser, pilih hanya
9 berkas paket distribusi; `.gitignore` berlaku untuk alur Git dan bukan filter
unggahan browser.

## Pemeriksaan

Periksa integritas bundel tanpa memasang dependensi:

```bash
python bundle_loader.py verify
```

Setelah dependensi dashboard terpasang, jalankan pengujian asli:

```bash
python bundle_loader.py test
```

Hasil verifikasi paket ini:

| Pemeriksaan | Hasil |
| --- | --- |
| Perbandingan isi bundel dengan ZIP masukan | Identik; SHA-256 sama |
| Perbandingan hasil ekstraksi dengan sumber asli | 806 dari 806 berkas identik |
| Suite pengujian asli sebelum pengemasan | 14 dari 14 lulus |
| Suite pengujian asli melalui pembuka bundel | 14 dari 14 lulus |
| Start pertama dari folder unggahan bersih | Ekstraksi otomatis dan dashboard berhasil |
| Tujuh halaman dengan Streamlit AppTest | Semua terbuka tanpa exception |
| Perpindahan halaman dan rerun | Berhasil; runtime digunakan kembali |
| Mode offline historis dan mode tanpa data | Berhasil |
| Ekspor proyek lengkap | 806 berkas identik; folder lama tidak ditimpa |

Pengujian lokal menggunakan Python 3.12.14 dan Streamlit 1.66.1. Skenario API
dalam suite bawaan memakai mock; koneksi ke backend live dan deployment pada
akun GitHub/Streamlit Anda belum dilakukan. Tidak ada transaksi sungguhan
yang dijalankan dalam pengujian ini.

## Catatan pengembangan

Sumber asli disimpan utuh di dalam ZIP supaya unggahan tetap ringkas. Perubahan
fitur selanjutnya dilakukan pada proyek yang diekspor. Paket yang telah diubah
perlu dibundel ulang dan nilai `BUNDLE_SHA256`/`ORIGINAL_FILE_COUNT` di
`bundle_loader.py` diperbarui; jangan mengganti isi ZIP saja. Pemakaian dashboard
sehari-hari tidak memerlukan langkah ini.
