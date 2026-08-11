"""高德 Web 服务：周边地点搜索。

这一层只回答一个问题：**以这个坐标为圆心、这个半径内，有哪些店。**

它不做筛选。"哪家合适"要用 logs 和 memories 判断，那是 cadence 自己的活
（设计文档 7.5：赢过美团的不是商家数据，是"你上周吃了三次辣、这家你说过
难吃"）。所以这个文件里不该出现任何跟个人偏好有关的逻辑。

不用 requests，标准库的 urllib 够了（设计文档 3.4）。
"""

import json
import time
import urllib.parse
import urllib.request

import config

# 周边搜索。高德还有关键字搜索、多边形搜索，用到再加。
AROUND_URL = "https://restapi.amap.com/v3/place/around"

# 地理编码：地址文字 -> 经纬度。省得人去地图界面上找坐标。
GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"

# 逆地理编码：经纬度 -> 行政区。只用来问"家在哪个市"。
REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"

# 高德的 POI 分类编码。全表见官方「POI 分类编码表」，这里只放会用到的。
# 前两位是大类，050000 = 整个餐饮服务大类。
TYPES = {
    "餐饮": "050000",
    "中餐": "050100",
    "快餐": "050300",
    "咖啡": "050500",
    "超市": "060400",
    "便利店": "060200",
}


class AmapError(Exception):
    """高德返回了失败状态。消息里带上它自己的说明，别自己编。"""


class GeocodeError(Exception):
    """一个地名解析不出能用的坐标。消息就是给人看的那句话。

    单独一个异常类型，是为了跟 AmapError 分开：AmapError 是"接口出问题了"，
    这个是"你说的这个地方我定位不了"，两者该说的话完全不同。
    """


# 并发/QPS 类的错误码。这类是"你太快了"，等一下重试就好，
# 跟"key 不对""额度用完了"完全不同 —— 后者重试多少次都没用。
# 实测触发点：翻页拉全量时连着发二十多个请求，第二个关键词就被拦。
_RETRYABLE = {"10021", "10019", "10020", "10029"}

RETRIES = 3
BACKOFF = 0.4       # 秒。每次重试翻倍


def _get(url: str, params: dict) -> dict:
    """发一次请求，检查高德自己的状态位。所有接口都从这里走。

    ⚠️ 高德的 HTTP 状态码永远是 200，成功与否要看 body 里的 status。
    只判断"请求发出去了"会把失败当成功。

    QPS 超限会自动退避重试（文档 12.2：故障出现在交界处，契约就要写在
    交界处）。其余错误立刻抛 —— key 填错了重试三次还是错，只是让你多等。
    """
    config.check_amap()
    params = {"key": config.AMAP_KEY, **params}
    full = f"{url}?{urllib.parse.urlencode(params)}"

    for attempt in range(RETRIES):
        with urllib.request.urlopen(full, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") == "1":
            return data
        code = str(data.get("infocode") or "")
        if code not in _RETRYABLE or attempt == RETRIES - 1:
            raise AmapError(f"{data.get('info')}（infocode {code}）")
        time.sleep(BACKOFF * (2 ** attempt))

    raise AmapError("重试用尽")      # 到不了，让类型检查和读者都安心


_city_cache: dict[str, str] = {}


def city_of(location: str) -> str:
    """这个坐标属于哪个市。用来给 geocode 限定范围，结果缓存在内存里。

    存在的理由是一个真出现过的 bug：在南京问"新街口"，geocode 不限城市，
    高德给了**四川乐山**的一个新街口（103.77,29.46）。之后所有推荐都是
    乐山的店，而屏幕上只写着"新街口"—— 你和模型都看不出来。

    地名重名是常态（新街口 / 中山路 / 人民广场哪个城市都有），所以这不是
    个别情况，是默认情况。
    """
    if location not in _city_cache:
        data = _get(REGEO_URL, {"location": location})
        comp = (data.get("regeocode") or {}).get("addressComponent") or {}
        city = comp.get("city") or comp.get("province") or ""
        # 直辖市的 city 字段是空列表，这时候要用 province
        _city_cache[location] = city if isinstance(city, str) else ""
    return _city_cache[location]


def geocode(address: str, city: str = "") -> list[dict]:
    """地址 -> 坐标。返回按高德的排序，第一条通常最靠谱。

    ⚠️ city 一定要传，除非地址里已经写了城市。见 city_of() 的注释：
    不限城市的"新街口"会解析到另一个省去，而且不报错。

    level 字段说明匹配到了多细的一级（"门牌号" / "兴趣点" / "道路" / "城市"…）。
    匹配到"城市"意味着它没找着你写的地方，只好给了个市中心 —— 这种坐标拿去
    查周边毫无意义，所以要把 level 带出来让人看见，不能只给个坐标假装成功。
    """
    data = _get(GEOCODE_URL, {"address": address, "city": city})
    return [
        {
            "location": g.get("location"),
            "formatted_address": g.get("formatted_address"),
            "level": g.get("level"),
        }
        for g in data.get("geocodes", [])
    ]


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


# 匹配得太粗，说明高德没找着你写的地方，只好给了个行政中心。
# 这种坐标拿去查周边毫无意义 —— 宁可报错，也不能静悄悄地返回一个错的。
#
# ⚠️ 这些字符串必须跟高德实际返回的 level 一字不差。之前写成"城市"，
# 而高德给的是"市"，于是这道拦截从写出来就是死的：查"南京"会拿市中心
# 跑 3 公里，一条错也不报。**照着真实响应写，别照着自己的直觉写。**
# 区县及更细的（区县 / 商圈 / 道路 / 兴趣点 / 门牌号）都放行。
_TOO_COARSE = ("国家", "省", "市")


def resolve(place: str, city: str | None = None) -> dict:
    """地名或坐标 -> {location, label, formatted_address, level, alternatives}。

    这是所有"用户说了个地方"的统一入口：CLI 和模型工具都走它，谁也别自己
    拼 geocode 调用。解析不了就抛 GeocodeError，**由调用方决定是打印退出
    还是回一句话给模型** —— 所以这里不 print、不 exit。

    city 不传就自动用家所在的市。地名重名是常态（新街口 / 中山路 / 人民广场
    哪个城市都有），不限范围的话高德会给你另一个省的同名地点，还不报错。
    传空字符串表示明确不限制。

    label 里带上高德解析出的完整地址：光显示"新街口"，你没法判断它去的是
    哪个新街口。这一路要显示到底，包括返回给模型的那一行。
    """
    place = (place or "").strip()
    if not place:
        raise GeocodeError("没说地方。")
    if looks_like_coords(place):
        return {"location": place, "label": place, "formatted_address": place,
                "level": "坐标", "alternatives": 0}

    if city is None:
        city = city_of(config.HOME) if config.HOME else ""

    hits = geocode(place, city=city)
    if not hits:
        where = f"在{city}" if city else ""
        raise GeocodeError(f"高德{where}找不到「{place}」。"
                           f"写详细点试试，比如带上区和路名。")

    best = hits[0]
    if best["level"] in _TOO_COARSE:
        raise GeocodeError(
            f"「{place}」只匹配到{best['level']}一级（{best['formatted_address']}），"
            f"太粗了，查周边没意义。地址写细一点，带上区和路名门牌号。")

    return {
        "location": best["location"],
        "label": f"{place}（{best['formatted_address']}）",
        "formatted_address": best["formatted_address"],
        "level": best["level"],
        "alternatives": len(hits) - 1,
    }


# 高德一页最多 25 条，要更多就得翻页。
PAGE_SIZE = 25
MAX_PAGES = 40      # 兜底：别让循环失控，一次最多 1000 条


def search_around(location: str, radius: int = 1000, types: str = "050000",
                  keywords: str = "", limit: int = 20,
                  sortrule: str = "distance") -> dict:
    """查周边地点。location 是 "经度,纬度"（经在前，高德的顺序）。

    返回 {"total": 圈内总数, "pois": [...]}。

    为什么要带上 total：pois 只是前 limit 条，光看它分不清"这一带只有 20 家"
    还是"有 200 家、你看到了最近的 20 家"。这两种情况该做的事完全不同。

    sortrule="distance" 按距离，"weight" 按高德的综合排序。
    ⚠️ 按距离排时，把半径调大不会看到新东西 —— 最近的那批永远最近。
      想看远处的店得靠 limit 翻页，或者换成 weight。
    """
    pois: list[dict] = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        if len(pois) >= limit:
            break
        params = {
            "location": location,
            "radius": radius,
            "types": types,
            "offset": PAGE_SIZE,      # 固定每页大小，否则翻页的页码算不准
            "page": page,
            "sortrule": sortrule,
            "extensions": "all",      # 不加这个就没有评分和人均
        }
        if keywords:
            params["keywords"] = keywords

        data = _get(AROUND_URL, params)
        if page == 1:
            # 总数只认第一页报的。翻到空页时 count 会变成 0，
            # 每页都覆盖的话最后一定得到 0 —— 这个 bug 犯过一次。
            total = int(data.get("count") or 0)
        batch = data.get("pois") or []
        if not batch:                 # 没有下一页了
            break
        pois.extend(_simplify(p) for p in batch)

    # ⚠️ 实测高德的深度翻页有上限：3 公里内它自己报 600 家，但翻到第 9 页
    # 就没有了，实际只能取回约 200 家。所以 len(pois) < total 是常态，
    # 不是 bug。凡是用得上"全部"的地方都必须按抽样对待。
    return {"total": total, "pois": pois[:limit]}


def search_keywords(location: str, keywords: str, radius: int = 3000,
                    types: str = "050000", limit: int = 40) -> dict:
    """按一个或多个关键词查（逗号分隔），合并去重。

    这是常用的那条路：一个关键词 1-2 次请求、不到一秒，因为高德那边先筛过了。
    对比之下 list_types() 要拉全量，二十多次请求、四到六秒。

    ⚠️ 高德不是语义搜索，但它也不老实地返回 0 条 —— 实测搜「辣的」，它会
    给你八家肯德基。**乱猜比查不到更危险**：查不到你会去改词，乱猜你会
    直接把肯德基当成辣菜推出去。

    所以返回里带一个 loose 标志：**关键词既没出现在任何结果的分类里、
    也没出现在任何店名里，就判定这次是瞎蒙。** 调用方看见 loose 就该丢掉
    结果、去看 list_types() 的目录，从里面挑真名重查（文档 7.4）。

    这个判据不需要维护任何同义词表 —— 词表一定会漏，而结果里的分类名是真的：
        「川菜」→ 结果里有「四川菜(川菜)」，含这两个字      → 可信
        「随园」→ 结果里有店名「随园餐厅(仙鹤门店)」        → 可信（按店名查）
        「日料」→ 高德那一类叫「日本料理」，没有一条含"日料" → loose
        「辣的」→ 分类和店名里都不含"辣的"                  → loose
    """
    seen: set[str] = set()
    merged: list[dict] = []
    total = 0
    loose = True
    for kw in [k.strip() for k in keywords.split(",") if k.strip()]:
        got = search_around(location, radius, types, kw, limit)
        total += got["total"]
        for poi in got["pois"]:
            if kw in (poi["type"] or "") or kw in (poi["name"] or ""):
                loose = False       # 只要有一个词真命中过，就不算瞎蒙
            # 同一家店可能被两个关键词都命中（火锅店也叫川味火锅）
            key = poi["id"] or poi["name"]
            if key not in seen:
                seen.add(key)
                merged.append(poi)

    merged.sort(key=lambda p: p["distance"] if p["distance"] is not None else 10**9)
    return {"total": total, "pois": merged[:limit], "loose": loose and bool(merged)}


def list_types(location: str, radius: int = 3000, types: str = "050000") -> dict:
    """这一带实际有哪些分类。返回 {"total": 圈内总数, "sampled": 实际取回数,
    "types": [(分类, 家数)…]}。

    ⚠️ 家数是**抽样**的：高德深度翻页有上限，3 公里内它报 600 家但只翻得出
    约 200 家。所以"中餐厅 81"意思是"取回的 200 家里有 81 家中餐厅"，
    不是这一带只有 81 家中餐厅。用途是"这一带有哪些菜系"，那 200 家的样本
    完全够；但**别拿这个数字去做任何比较或推断**。total 和 sampled 都返回，
    就是为了让调用方没法假装不知道这件事（文档 7.4：宁可说不知道，不许编）。

    这是"我今天中午吃什么"那种没有关键词的问法唯一该走的路。直接把几百家
    店返回去是不行的：实测一行式压缩后仍是 20 tok/条，600 家就是 1.1 万
    token，而且每轮对话都要重付一次（设计文档 12.1 的 token 账本）。
    统计成二三十个分类只有约 200 token。

    代价是要拉全量：二十多次请求、四到六秒。拉回来的几百家只活在这个函数
    的局部变量里，统计完就丢 —— 不落库、不进 prompt。

    它同时也是关键词落空时的兜底：把真实存在的分类名给模型，让它改用真名
    重查，而不是回一句"这边没有"（文档 7.4：降级而不是编）。
    """
    got = search_around(location, radius, types, limit=MAX_PAGES * PAGE_SIZE)
    counts: dict[str, int] = {}
    for poi in got["pois"]:
        t = poi["short_type"] or "未分类"
        counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return {"total": got["total"], "sampled": len(got["pois"]), "types": ranked}


# 高德的分类串形如「餐饮服务;中餐厅;四川菜(川菜)」，最细的一级有时是这几个
# 什么也没说的词。碰上就往回退一级，"餐饮相关"没用，"中餐厅"有用。
_VAGUE_TYPES = {"餐饮相关", "餐饮相关场所", "其他"}


def short_type(full: str) -> str:
    """从完整分类串里取最细的、有意义的那一级。给人和模型看的都用它。"""
    parts = [p for p in (full or "").split(";") if p.strip()]
    for p in reversed(parts):
        if p not in _VAGUE_TYPES:
            return p
    return parts[-1] if parts else ""


def _simplify(poi: dict) -> dict:
    """把高德的原始 POI 压成我们要的几个字段。

    ⚠️ rating 和 cost 经常是空的 —— 高德是地图公司，商户运营数据不是它的强项。
    所以这两个字段一律当"可能没有"处理，任何依赖它们的逻辑都要能接受 None。

    ⚠️ type 存完整串，不要在这里截断。之前做 split(";")[-1] 丢了分类层级，
    结果"高嗲嗲·湘味爆炒王"的分类显示成「餐饮相关」—— 有用的那一级被扔了。
    完整串还是关键词匹配的依据（"川菜"要能命中「…;四川菜(川菜)」）。
    要短的用 short_type()。
    """
    biz = poi.get("biz_ext") or {}

    def clean(v):
        # 高德把空值表示成 [] 或 ""，统一成 None，省得上层判断两种空。
        return v if isinstance(v, str) and v.strip() else None

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    full_type = poi.get("type") or ""
    return {
        "id": poi.get("id"),                              # 高德的 POI id，用来去重
        "name": poi.get("name"),
        "address": clean(poi.get("address")),
        "location": poi.get("location"),
        "distance": int(poi["distance"]) if poi.get("distance") else None,
        "type": full_type,                                # 完整串
        "short_type": short_type(full_type),              # 给人看的一级
        "typecode": poi.get("typecode") or "",
        "tel": clean(poi.get("tel")),
        "rating": num(biz.get("rating")),
        "cost": num(biz.get("cost")),
    }
