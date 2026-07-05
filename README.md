# selfmatrix-hires — ハイレゾ音声 (拡張オプション)

[SelfMatrix](https://github.com/zoobookfool/selfmatrix) (Matrix ベースの Discord 代替) 向けの、**本体とは結合しない独立した拡張モジュール**です。JackTrip (hub mode) で 192kHz/24bit の非圧縮ステレオ音声を VPS 中継します。

- 本体 (Synapse / LiveKit / クライアント fork) には一切依存しません。有効化しなくても SelfMatrix の通話・チャットは通常どおり動作しますし、SelfMatrix を使っていなくても単体で使えます。
- 設計判断の経緯 (スパイク記録・要件・方針変更) は親プロジェクトの [docs/hires-spike.md](https://github.com/zoobookfool/selfmatrix/blob/main/docs/hires-spike.md)、[docs/requirements.md](https://github.com/zoobookfool/selfmatrix/blob/main/docs/requirements.md) §4/§9、[docs/roadmap.md](https://github.com/zoobookfool/selfmatrix/blob/main/docs/roadmap.md) Phase 6 を参照してください。

## 1. これは何か

JackTrip (hub mode) による 192kHz/24bit の非圧縮ステレオ音声中継です。通常の通話 (WebRTC/Opus 48kHz) とは完全に別系統で動き、参加にはネイティブアプリ (`jacktrip` コマンド) が必要です。ブラウザだけでは参加できません。

用途は「通話音声そのものの高音質化」です。BGM 再生や音楽鑑賞ではなく、会話の音質向上が主目的です。

## 2. 必要なもの

- **ヘッドセット必須。** JackTrip hub にはエコーキャンセル機能がありません。スピーカーで参加すると自分の声が相手側にループして返り、ハウリング・エコーの原因になります。必ずヘッドセット (マイク付きイヤホン/ヘッドホン) を使ってください。
- 192kHz 対応のオーディオインターフェースを推奨します。**サンプルレートは hub と参加者全員で一致している必要がある** ため、非対応の参加者が混ざる回は運用者が hub を 48kHz で建て直して全員 `-T 48000` で参加します (§3.1 の `--sample-rate`)。
- Windows/macOS/Linux いずれかの PC (スマートフォンは非対応)。

## 3. サーバー側セットアップ (運用者向け)

### 3.1 provision スクリプト

`provision.sh` がステートレスな VPS (Ubuntu 想定) 上に hub 一式 (jackd + jacktrip + systemd unit + ufw ルール) を組み立てます。

```sh
git clone https://github.com/zoobookfool/selfmatrix-hires.git
cd selfmatrix-hires

# 内容を確認したいだけなら (何も変更しません)
sudo bash provision.sh --dry-run

# 実行 (既定: 同時最大 6 人、TCP 4464、サンプルレート 192000Hz)
sudo bash provision.sh

# 上限や待受ポート・サンプルレートを変える場合
sudo bash provision.sh --max-clients 4 --sample-rate 48000
```

再実行しても安全です (既に存在するユーザー・credsfile・TLS 証明書は上書きしません)。ポートや上限を変えて再実行した場合、稼働中サービスへの反映には restart が、古い ufw ルールの掃除には `ufw status numbered` → `ufw delete <n>` が必要です (スクリプトが都度案内します)。

接続先ホスト名は **CDN プロキシを通さない DNS レコード** (いわゆる DNS-only) で VPS に直接向けてください。JackTrip の UDP メディアは CDN を通りません。

### 3.2 参加者の追加/削除

参加者の追加は provision とは別に、いつでも実行できます。

```sh
# パスワードを自動生成 (結果に一度だけ表示されるので、その場で控えて安全な経路で参加者に渡す)
sudo bash provision.sh --add-user alice

# パスワードを指定する場合
sudo bash provision.sh --add-user alice --password '<好きなパスワード>'
```

削除は credsfile (`/etc/selfmatrix-hires/credentials`) から該当行を手で消し、即時反映させたい場合は hub サービスを再起動してください。

```sh
sudo systemctl restart selfmatrix-hires-hub.service
```

### 3.3 起動/停止 (オンデマンド運用)

RAM 節約のため、systemd unit は既定で **enable されていません** (VPS 起動時に自動起動しません)。使うときだけ起動してください。

```sh
sudo systemctl start selfmatrix-hires-jack.service selfmatrix-hires-hub.service

# 使い終わったら
sudo systemctl stop selfmatrix-hires-hub.service selfmatrix-hires-jack.service
```

hub のオーディオスタックは接続人数に応じて RAM を消費します (実測: 2 クライアント接続時で ≈400MB)。1GB クラスの VPS では常時起動せず使うときだけ起動する運用にし、既定上限 6 人〜10 人規模を日常的に回すなら 2GB クラスを前提にしてください。

## 4. 参加者のクライアント導入

### 4.1 インストール

- **Windows / macOS**: [jacktrip.github.io/jacktrip/Install](https://jacktrip.github.io/jacktrip/Install/) の公式ページからインストーラをダウンロードして実行してください (winget/Chocolatey パッケージは存在しません)。
- **Linux**: apt が使える環境ならパッケージから入ります。

  ```sh
  sudo apt-get install -y jacktrip
  ```

### 4.2 接続コマンド

```sh
jacktrip -C <hires ホスト名> -T 192000 -b 24 -n 2 --udprt -R -A \
  --username <あなたのユーザー名> --password <パスワード>
```

各フラグの意味:

| フラグ | 意味 |
| --- | --- |
| `-C <host>` | 接続先ホスト (運用者から案内された hires 用ホスト名) |
| `-T 192000` | サンプルレート。**hub 側 (既定 192000) と一致させる必要があります**。192kHz 非対応の IF の参加者がいる回は、運用者が `--sample-rate 48000` で hub を建て直し、全員が `-T 48000` で参加します |
| `-b 24` | ビット深度。**hub 側と一致していないと即切断されます**。変更しないでください |
| `-n 2` | チャンネル数 (ステレオ) |
| `--udprt` | UDP のリアルタイム優先設定 |
| `-R` / `--rtaudio` | OS 標準のオーディオシステムを直接使用 (JACK のインストール不要) |
| `-A` | 認証を使う (hub が認証必須のため必須) |
| `--username` / `--password` | 運用者から発行されたアカウント。省略すると標準入力から聞かれます |

オーディオデバイスの指定・確認:

```sh
# 使えるオーディオデバイス一覧を表示
jacktrip --listdevices

# デバイスを指定して接続
jacktrip -C <hires ホスト名> -T 192000 -b 24 -n 2 --udprt -R -A \
  --audiodevice "<デバイス名>" --username <あなたのユーザー名> --password <パスワード>
```

**Windows で 192kHz を狙う場合は、オーディオインターフェースのメーカー公式 ASIO ドライバの使用を推奨します** (ASIO4ALL は公式 KB 上でも非推奨とされています)。

## 5. 運用ルール

### 5.1 二重再生防止

ハイレゾ音声と本体の通話 (WebRTC) は完全に独立した別系統のため、両方に同時参加すると同じ相手の声が二重に聞こえます (エコー・ハウリングの原因)。ハイレゾ参加中は次のいずれかを徹底してください。

- 本体のボイスチャンネルの**自分の出力をミュートする** (相手の声だけ止める。自分のマイクは切らなくて構いません)
- または本体のボイスチャンネルから**退出**し、画面共有やテキストチャットだけ本体側で使う

クライアント側での自動ミュート実装は行っていません (本体と結合しない方針のため)。運用ルールの周知で代替してください。

### 5.2 人数上限とその理由

192kHz/24bit ステレオの非圧縮 PCM は 1 人あたり上り/下りそれぞれ約 9.8Mbps 消費します (実測値、[親プロジェクトのスパイク記録](https://github.com/zoobookfool/selfmatrix/blob/main/docs/hires-spike.md) 参照)。VPS の回線帯域 (多くの共有 VPS プランで 100Mbps 程度) を踏まえ、**既定の上限は同時 6 人**です (`--max-clients` で調整可能ですが、回線容量以上には増やさないでください)。

また、**ハイレゾ hub は同時 1 セッションのみ** (サーバー全体でミックスは 1 つ) を前提にしています。JackTrip hub は接続者全員が同じミックスに入る構造であることに加え、上記の帯域試算のとおり複数セッションを並走させる回線余裕が無いためです。どうしても複数セッションが必要になった場合は `--bind-port` を変えて hub を増設できますが、回線増強とセットで判断してください。

## 6. トラブルシュート

### 接続できない

- **`-b` (ビット深度) の不一致**: hub 側は `-b 24` 固定です。クライアントも `-b 24` を指定してください。不一致だと即切断されます。
- **`-T` (サンプルレート) の不一致**: hub 側の既定は 192000 です。クライアントの `-T` を一致させてください。
- **認証エラー**: `--username`/`--password` が運用者に登録してもらったものと一致しているか確認してください。運用者側は credsfile に該当ユーザーが追記されているか確認します。
- **証明書エラーで弾かれる場合**: hub の TLS 証明書は既定で自己署名です。クライアント側で証明書検証エラーが出て接続できない場合は、運用者に正規の証明書 (Let's Encrypt 等) への切替を依頼してください (hires ホストが DNS で解決できる前提が必要です)。
- **MTU / パケット断片化**: hub 側は JACK 周期 (バッファサイズ) を既定で 128 に設定しています。192kHz・ステレオ・24bit だと 1 パケットが 784byte 程度に収まり、一般的な MTU (1500) 内に収まります。周期を大きくするとパケットがフラグメント化し、経路によっては (WSL2 の NAT など) 接続が "Waiting for Peer..." のまま止まることがあります。バッファサイズは変更しないことを推奨します。

### 音が途切れる

- クライアント接続コマンドの `-q` (キューサイズ、hub 側は既定 8) を増やしてジッタ耐性を上げてください。レイテンシは若干増えますが、途切れより優先すべき場面では有効です。
- 回線が混雑している (人数上限に近い、または本体の通話と回線を共有している) 可能性があります。§5.2 の人数上限を見直すか、時間帯をずらしてください。

## ライセンス

AGPL-3.0 (親プロジェクトと同一)。[LICENSE](LICENSE) を参照してください。
