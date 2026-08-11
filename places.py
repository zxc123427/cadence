#!/usr/bin/env python3
"""查附近有什么店，带上"这家我去过没有"。

    python3 places.py 川菜                    # 家附近的川菜
    python3 places.py 川菜,湘菜,火锅           # 多个关键词，合并去重
    python3 places.py 火锅 --at 新街口         # 换个中心点，地址自动转坐标
    python3 places.py 火锅 --max-cost 60 --min-rating 4
    python3 places.py --types                 # 这一带实际有哪些分类
    python3 places.py --where 新街口           # 只查坐标，不搜店

关键词必须是**真实存在的分类名或店名**。高德那边是文本匹配不是语义搜索，
「辣的」「清淡」这种描述词一条都搜不出来 —— 先 --types 看目录，从里面挑。

这个脚本替代了原来的 nearby.py：那个名字只讲了"附近"，但它还要管
地址解析和分类目录，跟 meals.py / remember.py 一样按概念命名更清楚。
"""

import argparse
import sys

import amap
import config
import db

COORD_HINT = """坐标怎么来（两条路，随便哪条）：
  1. python3 places.py --where 上海市黄浦区思南路36号
  2. 高德官方坐标拾取器 https://lbs.amap.com/tools/picker
⚠️ 一定从高德取。别的地图坐标系不一样，直接抄过来差几百米。"""


def looks_like_coords(s: str) -> bool:
    """"121.47,31.23" 这种算坐标，其余一律当地址去查。"""
    parts = s.split(",")
    if len(parts) != 2:
        return False
    try:
        float(parts[0]), float(parts[1])
    except ValueError:
        return False
    return True


def resolve(place: str, quiet: bool = False) -> tuple[str, str]:
    """把用户给的东西变成坐标，返回 (坐标, 说清楚是哪儿的名字)。

    默认限定在家所在的城市里找 —— 不限的话"新街口"会解析到别的省去，
    而且不报错（见 amap.city_of）。
    """
    if looks_like_coords(place):
        return place, place

    # 家在哪个市，就在哪个市里找。地址里自己写了城市的，高德会以地址为准。
    city = amap.city_of(config.HOME) if config.HOME else ""
    hits = amap.geocode(place, city=city)
    if not hits:
        where = f"在{city}" if city else ""
        print(f"高德{where}找不到「{place}」。换个更完整的写法试试，比如带上区和路名。")
        sys.exit(1)

    best = hits[0]
    # level 太粗说明它没找着，只好给了个市中心 —— 这种坐标查周边没意义，
    # 与其静悄悄地返回一个错的，不如停下来说清楚。
    if best["level"] in ("城市", "省", "国家"):
        print(f"「{place}」只匹配到{best['level']}一级，坐标是 {best['formatted_address']}。")
        print("太粗了，查周边没意义。地址写细一点，带上区和路名门牌号。")
        sys.exit(1)

    if not quiet:
        print(f"「{place}」→ {best['location']}"
              f"（{best['formatted_address']}，匹配到{best['level']}）")
        if len(hits) > 1:
            print(f"（还有 {len(hits) - 1} 个同名结果，用的是第一个。不对就写详细点）")

    # 名字带上高德解析出的完整地址：重名的地方太多，光显示"新街口"
    # 你没法判断它去的是哪个新街口。
    return best["location"], f"{place}（{best['formatted_address']}）"


def show_where(place: str) -> None:
    hits = amap.geocode(place)
    if not hits:
        print(f"高德找不到「{place}」。换个更完整的写法试试，比如带上市和区。")
        return
    print(f"「{place}」的匹配结果：\n")
    for h in hits:
        print(f"  {h['location']}   {h['formatted_address']}  [{h['level']}]")
    print(f"\n把它填进 .env：\n    CADENCE_HOME={hits[0]['location']}")


def show_types(location: str, radius: int) -> None:
    print("（要拉全量统计，二十多次请求，等几秒…）")
    got = amap.list_types(location, radius)
    print(f"\n{radius} 米内共 {got['total']} 家餐饮，"
          f"抽到 {got['sampled']} 家，分类如下：\n")
    for name, n in got["types"]:
        print(f"  {n:>4}  {name}")
    print("\n关键词就从这一列里挑 —— 高德只认这些真名。")
    if got["sampled"] < got["total"]:
        print(f"⚠️ 家数是这 {got['sampled']} 家里的分布，不是全量"
              f"（高德深度翻页有上限）。看有哪些菜系够用，别拿数字做比较。")


def history_note(name: str) -> str:
    """这家店我去过没有。空字符串表示没记录。"""
    rows = db.place_history(name)
    if not rows:
        return ""
    last = rows[0]
    note = f"去过{len(rows)}次，最近 {db.to_local(last['ts'])}"
    if last["note"]:
        note += f"，你说过「{last['note']}」"
    return note


def show_places(location: str, label: str, keywords: str, radius: int,
                max_cost: float | None, min_rating: float | None,
                limit: int) -> None:
    got = amap.search_keywords(location, keywords, radius, limit=limit)
    pois = got["pois"]

    # 筛掉不合要求的。⚠️ 评分/人均缺失的一律保留 —— 缺数据不是缺点，
    # 把它们悄悄筛掉等于让一家店因为高德没收录而消失。
    if max_cost is not None:
        pois = [p for p in pois if p["cost"] is None or p["cost"] <= max_cost]
    if min_rating is not None:
        pois = [p for p in pois if p["rating"] is None or p["rating"] >= min_rating]

    if not pois:
        print(f"没找到「{keywords}」。")
        print("看看这一带实际有什么：python3 places.py --types")
        return

    if got["loose"]:
        # 高德返了东西，但没有一条的分类或店名真的含这个词 —— 它在瞎蒙。
        # 直接把这批扔掉，别让人（或模型）拿八家肯德基当辣菜。
        print(f"「{keywords}」高德认不出来，返回的 {len(got['pois'])} 家里"
              f"没有一家的分类或店名真含这个词，全是蒙的，已丢弃。")
        print("换成真的分类名再查。看这一带有哪些：python3 places.py --types")
        return

    print(f"以「{label}」为中心 {radius} 米内的「{keywords}」，{len(pois)} 家：\n")
    for p in pois:
        rating = f"  ★{p['rating']}" if p["rating"] else ""
        cost = f"  ¥{p['cost']:.0f}" if p["cost"] else ""
        print(f"  {p['distance']:>5}m  {p['name']}{rating}{cost}")
        print(f"         {p['short_type']}  ·  {p['address'] or '无地址'}")
        if been := history_note(p["name"]):
            print(f"         ← {been}")


def main() -> None:
    p = argparse.ArgumentParser(description="查附近的店")
    p.add_argument("keywords", nargs="?", default="",
                   help="分类名或店名，逗号分隔可给多个。必须是真名，见 --types")
    p.add_argument("--where", metavar="地址", help="只查这个地址的坐标，不搜店")
    p.add_argument("--types", action="store_true", help="列出这一带实际有哪些分类")
    p.add_argument("--at", help="中心点，坐标或地址。不填就用 .env 的 CADENCE_HOME")
    p.add_argument("--radius", type=int, default=3000, help="半径，米（默认 3000）")
    p.add_argument("--max-cost", type=float, metavar="元", help="人均不超过")
    p.add_argument("--min-rating", type=float, metavar="分", help="评分不低于")
    p.add_argument("--limit", type=int, default=40, help="最多显示几条")
    args = p.parse_args()

    if args.where:
        return show_where(args.where)

    place = args.at or config.HOME
    if not place:
        print("没说查哪儿。用 --at 给个地址或坐标，")
        print("或者在 .env 里加一行 CADENCE_HOME=经度,纬度")
        print()
        print(COORD_HINT)
        sys.exit(1)

    if not args.types and not args.keywords:
        print("要查什么？给个分类名或店名，比如：")
        print("    python3 places.py 川菜")
        print("不知道有哪些就先看目录：")
        print("    python3 places.py --types")
        sys.exit(1)

    try:
        location, label = resolve(place)
        print()
        if args.types:
            show_types(location, args.radius)
        else:
            show_places(location, label, args.keywords, args.radius,
                        args.max_cost, args.min_rating, args.limit)
    except amap.AmapError as e:
        print(f"高德拒绝了这次请求：{e}")
        if "LIMIT" in str(e) or "EXCEED" in str(e):
            print("这是配额/并发类的错误，不是 key 的问题。等一会儿再试，"
                  "或者去 lbs.amap.com 控制台看今天的用量。")
        else:
            print("常见原因：key 的平台类型不是「Web 服务」，或者 key 填错了。")
        sys.exit(1)
    except OSError as e:
        print(f"网络请求失败：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
