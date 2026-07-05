# Stage 2 圧縮ゲートウェイ PoC — 段階1(ローカル/WSL統合テスト)手順書

本書は `docs/stage2-compression-gateway.md` §7.1「段階1: ローカル(WSL/同一マシン内)」の実行手順です。
設計の正本は同文書であり、本書はその PoC 実装(`gateway/`)を動かすための実務手順に限定します。
数値目標・検証項目は設計文書 §7.1 に従います。

## 0. スコープと制限事項(最初に必ず読むこと)

- **平文ハンドシェイクのみ対応**。JackTrip hub に `-A`(認証)を付けない構成が対象です。
  `-A` を使う認証モードでは、TCP 4464 ハンドシェイクは平文4byteの `Auth::OK` 往復の直後に
  TLS へアップグレードし、実際のポート番号・ユーザー名・パスワードは **TLS レコードの内側**
  でやり取りされます。本 PoC のプロキシは透過的にバイトを中継するだけ(TLS の中身は読めない)
  なので、認証モードでは UDP ポートのパッシブ学習が機能しません。TCP の中継自体(バイト列の
  素通し)は生き続けるため JackTrip 同士の接続は成立しうる可能性がありますが、UDP セッション
  対応表が構築されないため本プロキシは機能しません。認証モードに対応させるには、プロキシが
  hub と同じ証明書(`/etc/selfmatrix-hires/tls.{crt,key}` 相当)で **TLS を終端する MITM 構成**
  が必要になります。これは将来の課題であり、本 PoC には含まれません。
- 平文モードであっても、TCP ハンドシェイクでクライアントが送る4byteの「UDPポート」値は
  hub 側で無視されるダミーです(バイトレベル調査で確定)。本プロキシが実際に使うのは
  **hub→クライアントの4byte応答(hubが割り当てたUDPポート番号)** だけです。
- セッションは `port_tag = hub割当UDPポート & 0xFFFF` で識別する簡易実装です。複数クライアント
  同時接続時の競合状態やタグ衝突は本 PoC のスコープ外です(設計文書 §8 既知の未解決点)。
- WavPack コーデックは `wavpackdll.dll` / `libwavpack.so` を ctypes でロードして使います。
  本 PoC 制作時点では実行環境に当該ライブラリが存在せず(scratchpad 配下は CLI exe のみ)、
  **wavpack コーデックの実機ラウンドトリップ検証は未実施**です。zlib コーデックは標準ライブラリ
  のみで完結し、selftest で実際に1000パケット×6条件のビット一致を確認済みです。

## 1. 前提環境

実行環境調査の結果、次が確認済みです。

- WSL(Ubuntu, WSL2, 既定ディストリ)に `jacktrip 2.2.2`・`jackd/jackdmp 1.9.21` がインストール済み
- WSL の `python3` は 3.12.3、Windows 側は `py`(3.13.5)/`python`(3.10.6)が利用可能
- `jack_capture` は未インストール(本 PoC の必須項目には含まれないため対応不要)
- WSL の `sudo` はパスワード要求のため非対話インストール不可。追加パッケージが必要な場合は
  事前に手動インストールしておくこと

## 2. selftest(ネットワーク不要)の実行

統合テストの前に、必ずこちらを実行して全 PASS を確認してください。

```bash
cd gateway
python3 selftest.py   # または Windows 側: py selftest.py
```

wavpack コーデックが動く環境(`wavpackdll.dll` / `libwavpack.so` を用意できた場合)では、
環境変数で明示的にパスを指定して実行します。

```bash
# Windows
set GATEWAY_WAVPACK_LIB=C:\path\to\wavpackdll.dll
py selftest.py

# WSL/Linux
export GATEWAY_WAVPACK_LIB=/path/to/libwavpack.so.1
python3 selftest.py
```

ライブラリが見つからない場合、wavpack 関連の項目は FAIL ではなく `SKIP` と表示されます
(selftest.py はライブラリ不在を許容し、必須なのは zlib のみです)。

## 3. ローカル統合テストの構成

設計文書 §3.1 の構成図をローカル1台(または WSL⇔Windowsホスト間)に圧縮して再現します。
JACK は `dummy` ドライバを使い、実オーディオデバイスなしで音声データの流れだけを検証します。

```
[WSL: hub 側]                         [WSL: client 側]
jackd (dummy, 192kHz, -p128, hub用サーバー名)
  └─ jacktrip -S -p 2  (TCPポート: 14464、認証なし)
       ↕ localhost
  gateway.py --role hub \
    --local-tcp-port 14464 \       (client側ゲートウェイからのTCP中継を受ける)
    --jacktrip-host 127.0.0.1 --jacktrip-tcp-port 14464 \
    --tunnel-port 15000 \
    --codec zlib --debug-headers 5
                                        gateway.py --role client \
                                          --local-tcp-port 4464 \    (jacktripクライアントが繋ぐ)
                                          --peer 127.0.0.1:15000 \
                                          --peer-tcp-port 24464 \
                                          --tunnel-port 15001 \
                                          --codec zlib --debug-headers 5
                                        jackd (dummy, 192kHz, -p128, client用サーバー名)
                                          └─ jacktrip -c 127.0.0.1 -C 4464 (client)
```

**注意**: 上記は「同一マシン上で hub 用/client 用の jackd を JACK_DEFAULT_SERVER で分離しつつ
localhost で完結させる」想定の論理構成です。gateway.py の `--role hub` は「クライアント側
ゲートウェイからの TCP 接続を受けて実 hub へ中継する」ためのリスナーを持つため、
`--peer-tcp-port` に指定するポート(上記例では 24464)を hub 側ゲートウェイの
`--local-tcp-port` と一致させる必要があります。

### 3.1 jackd を dummy ドライバで2系統起動する(WSL)

```bash
# 端末1: hub 用 JACK サーバー
JACK_DEFAULT_SERVER=hub_srv jackd -d dummy -r 192000 -p 128 &

# 端末2: client 用 JACK サーバー (同一マシンで分離するため別名にする)
JACK_DEFAULT_SERVER=client_srv jackd -d dummy -r 192000 -p 128 &
```

### 3.2 JackTrip hub を起動する(認証なし、TCPポートは既定の4464からずらす)

```bash
JACK_DEFAULT_SERVER=hub_srv jacktrip -S -p 2 -B 14464 -q 8
```

(`-B` は JackTrip の bind port オプション。既定の4464はゲートウェイのclient役が
占有するため、hub 実体は別ポートで待ち受けさせ、gateway.py --role hub の
`--jacktrip-tcp-port` でそこを指す。)

### 3.3 両ゲートウェイを起動する

```bash
# hub 側ゲートウェイ(hub と同居する想定のプロセス)
cd gateway
python3 gateway.py --role hub \
  --local-tcp-port 24464 \
  --jacktrip-host 127.0.0.1 --jacktrip-tcp-port 14464 \
  --tunnel-port 15000 \
  --codec zlib --debug-headers 5

# client 側ゲートウェイ(参加者PCと同居する想定のプロセス、別端末)
cd gateway
python3 gateway.py --role client \
  --local-tcp-port 4464 \
  --peer 127.0.0.1:15000 --peer-tcp-port 24464 \
  --tunnel-port 15001 \
  --codec zlib --debug-headers 5
```

### 3.4 JackTrip クライアントを起動する(client 側ゲートウェイへ接続)

```bash
# -C は「Hub Client モード + 接続先ホスト」。-c(P2P Client)とは排他なので併用しない。
# TCP は既定の 4464 番で client 側ゲートウェイ(127.0.0.2)へ繋がる。
JACK_DEFAULT_SERVER=client_srv jacktrip -C 127.0.0.2 -B 24465 -q 8 -L 127.0.0.2
```

接続が成立すると、client 側ゲートウェイの標準エラー出力に
`client role: learned hub udp port ... -> session tag ...` のログが出て、
client→hub 方向の UDP パケット中継が始まり、5秒おきに統計行が出力されます。

### 3.5 検証済みの構成と単一マシンの制限(2026-07-06)

上記手順で実際に検証した結果(§`docs/stage2-compression-gateway.md` §7.1 に詳細):

- **hub 側=127.0.0.1、client 側=127.0.0.2 の IP 分離**が必須。JackTrip は UDP をデュアルスタックのワイルドカード(`[::]:port`)でバインドするため、hub 側と client 側を同一ループバック IP に載せるとポートが衝突する。
- TCP ハンドシェイク透過中継・両ゲートウェイの session tag 一致・ヘッダ 16byte 素通し(mismatches=0)・zlib/wavpack の可逆圧縮は、実 JackTrip で確認済み(client→hub 方向)。
- **双方向(hub→client)の完全疎通は単一マシンでは未達**。IP 分離をしても実 hub の `JackTripWorker` が `Could not bind UDP socket` でクラッシュする(client 側ゲートウェイが同ポートを保持するため)。これは実運用(hub=VPS、client=参加者PC の2ホスト)では起きない単一マシン固有の制約であり、双方向の完全検証は段階2(VPS)で行う。
- `-B` を既定の 4464 から大きくずらすと、hub の UDP base port が連動して 65535 を超え、割当ポートが下位16bitに折り返る(例: `-B 14464` → base 71002 → 通知ポート 5466)。テストでは無害だったが、hub の bind port は既定付近に留めるのが無難。

### 3.6 バッチ圧縮を有効にして試す

両ゲートウェイに `--batch N`(と任意で `--batch-flush-ms M`)を付けると、N パケットをまとめて圧縮する(圧縮率の根拠は `docs/stage2-compression-gateway.md` §4.3/§5.1)。既定は N=1(バッチ無効)。実測では N≈16 が圧縮効果の頭打ち点。

```bash
# 例: 両ゲートウェイを --batch 16 で起動
python3 gateway.py --role hub   ... --codec zlib --batch 16
python3 gateway.py --role client ... --codec zlib --batch 16
```

dummy driver は純粋なゼロ無音のため圧縮率は非現実的に良く出る(N=1 で ratio≈7.4%、N=16 で ≈2.7%)。実音声での代表値は §4.3 の表を参照。`mismatches=0` が維持されることを確認する。

## 4. 確認項目(設計文書 §7.1 の5項目との対応)

| # | 設計文書 §7.1 の検証項目 | 本 PoC での確認方法 |
|---|---|---|
| 1 | TCP 4464ハンドシェイクの透過中継がTLS ClientHello判定と衝突しない | 平文モードのみ対応(本書§0)。TCP中継は生バイトのspliceであり、JackTrip側のTLS判定ロジック(先頭3byte確認)に一切手を加えないため、原理的に衝突しない。`--debug-headers` はUDP側の確認用であり、TCP側は`gateway.py`のログ(`[gateway] client role: learned hub udp port ...`)がハンドシェイク成立の証跡になる |
| 2 | VPS側プロキシがTCPストリームから割当UDPポートを正しく学習し、1:1中継が確立する | 上記ログの `session tag` が hub 側・client 側で同一の値になっていることを確認する。`gateway/selftest.py` の「handshake sniffer」テストで、分割着信・結合着信いずれでも正しく4byteポートを抽出できることを事前にユニットテスト済み |
| 3 | ヘッダ(16byte)が一切変更されず素通しされている | `--debug-headers N` で hub側・client側それぞれの最初のN個のヘッダを16進ダンプし、対応するTimeStamp/SeqNumberの値が両ログで一致することを目視確認する。selftest.py の「header passthrough」テストで、encode→decodeを通しても1bitも変わらないことをユニットテスト済み |
| 4 | ペイロード部の圧縮/伸長が可逆(ビット一致) | selftest.py の「codec round-trip bit-exactness」で、24bit×2ch×128 と float32×2ch×128 の乱数/正弦波/無音各1000パケットのビット一致を確認済み。統合テストでは、jacktrip側でXRUNや音割れ・ノイズが出ないことで間接的に確認する(将来的にはloopback録音での波形比較が必要、設計文書§7.2で実施) |
| 5 | 192kHz/24bit・周期128でのJackTrip接続が成立し、XRUNが発生しない | `jacktrip`の標準出力・`jackd`のログでXRUN報告がないことを確認する。長時間(数分)接続を維持し、ゲートウェイの統計行(`[stats] ...`)で `mismatches` が増え続けていないことを確認する |

## 5. 統計の読み方

ゲートウェイは5秒ごとに次の形式で標準エラーへ1行出力します。

```
[stats] pkts=1500 in=784000B out=312400B ratio=39.8% mismatches=0
```

- `pkts`: 直近5秒間に中継したパケット数
- `in` / `out`: 圧縮前 / 圧縮後(トンネルフレーム込み)の合計バイト数
- `ratio`: `out/in × 100%`。値が小さいほど帯域削減効果が大きい(設計文書 §4.1 と同じ定義)
- `mismatches`: ヘッダの自己記述検算(`BufferSize × channels × bytes_per_sample`)が実際の
  ペイロード長と一致しなかった回数。JackTrip起動直後の制御パケットやハンドシェイク残骸が
  UDPソケットに混入した場合にここが増えることがあるが、`--codec none`以外を選んでいても
  この場合は自動的に素通し(passthrough)にフォールバックするため、圧縮エラーで接続断には
  ならない

## 6. 制限事項(まとめ)

- 平文ハンドシェイクのみ対応。`-A` 認証モードは非対応(本書§0、TLS終端MITMが必要)
- wavpack コーデックは ctypes 実装のみでロジックレベルの静的検証(公式 `wavpack.h` /
  CLI実装との突き合わせ)は完了しているが、実ライブラリでの動作確認は本 PoC の作成時点では
  未実施。selftest.py は `GATEWAY_WAVPACK_LIB` 環境変数でライブラリを指定すれば自動的に
  ラウンドトリップ検証を行う(未指定・未検出時はSKIPし、FAILにはしない)
- セッション識別は `hub割当UDPポート & 0xFFFF` の単純な方式。複数クライアントの同時接続・
  再接続時の競合状態は未検証(設計文書§8)
- 本 PoC は段階1(ローカル/WSL)のみを対象とし、実WAN経路・実測150ms遅延・VPS実機CPU/RAM
  (設計文書§7.2 段階2)は別途実施が必要
- RT優先度・QoS設定は行っていない(設計文書§8 RT優先度設定の可否)
