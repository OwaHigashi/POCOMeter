# POCOMeter

Pococha のランクメーターの非公式予測ツールです。D1〜S6 の各ランクと 13 時・22 時・24 時の締め時間について、±0・+1・+2 に必要な応援ポイントの予測と、取得済みの公開実績を表示します。日付と締め時間は日本時間（JST）です。

## ローカルで動かす

Python 3.9 以降と PHP 8.1 以降が必要です。データの更新には Unix 系の環境と、外部のデータ取得元への接続が必要です。リポジトリのルートで次を実行します。

```sh
python3 -B update_meter.py
php -S 127.0.0.1:8000
```

ブラウザーで `http://127.0.0.1:8000/` を開きます。更新スクリプトは予測と実績を `data.json` に、更新状況を `status.json` に保存します。`events.json` は手動で確認した公式イベント情報です。

更新には、各ランク・締め時間で直近30日間の実績が7件以上あり、最新の実績が4日以内であることが必要です。取得に失敗した場合は、前回生成した予測があれば表示を続けます。

## JSON API と CSV

| パス | 内容 |
| --- | --- |
| `api.php` | 対象期間、対応ランク、締め時間、リクエスト例を返します。 |
| `api.php?date=YYYY-MM-DD&rank=B1&close=13` | 指定した日付・ランク・締め時間の予測、実績、イベント情報を返します。 |
| `api-csv.php` | データ上の当日について、全ランク・全締め時間を CSV で返します。`date`、`rank`、`close` で絞り込めます。 |

## データの出典

- ランクメーターの公開実績。[推しPメーター](https://www.oshi-p-meter.com/borders)
- 公式イベントの情報。[Pococha 公式イベントカレンダー](https://report.pococha.com/n/n803868883e7d)
- 補足のイベント予定。[配信予定メーカー（非公式）](https://yotei.ember-w.com)
- 日本の祝日・振替休日。[内閣府](https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html)

## コントリビューター

尾和 東　（おわ）
ぽこちゃ演奏枠

[OwaHigashi](https://github.com/OwaHigashi)
