import math
from typing import Set

import cv2
import numpy as np
from cv2.typing import MatLike

from basic import os_utils
from basic.img import cv2_utils, MatchResultList, MatchResult
from basic.img.os import save_debug_image
from basic.log_utils import log
from sr import constants
from sr.config.game_config import MiniMapPos, get_game_config
from sr.constants.map import Region
from sr.image import ImageMatcher, TemplateImage
from sr.image.sceenshot import MiniMapInfo


def cal_little_map_pos(screen: MatLike) -> MiniMapPos:
    """
    计算小地图的坐标
    通过截取屏幕左上方部分 找出最大的圆圈 就是小地图。
    部分场景无法准确识别 所以使用一次校准后续读取配置使用。
    最容易匹配地点在【空间站黑塔】【基座舱段】【接待中心】传送点
    :param screen: 屏幕截图
    """
    # 左上角部分
    x, y = 0, 0
    x2, y2 = 340, 380
    image = screen[y:y2, x:x2]

    # 对图像进行预处理
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, 1.2, 100, minRadius=80, maxRadius=100)  # 小地图大概的圆半径

    # 如果找到了圆
    if circles is not None:
        circles = np.uint16(np.around(circles))
        tx, ty, tr = 0, 0, 0

        # 保留半径最大的圆
        for circle in circles[0, :]:
            if circle[2] > tr:
                tx, ty, tr = circle[0], circle[1], circle[2]

        mm_pos = MiniMapPos(tx, ty, tr)
        log.debug('计算小地图所在坐标为 %s', mm_pos)
        return mm_pos
    else:
        log.error('无法找到小地图的圆')


def cut_mini_map(screen: MatLike, mm_pos: MiniMapPos = None):
    """
    从整个游戏窗口截图中 裁剪出小地图部分
    :param screen: 屏幕截图
    :return:
    """
    if mm_pos is None:
        mm_pos = get_game_config().mini_map_pos
    return screen[mm_pos.ly:mm_pos.ry, mm_pos.lx:mm_pos.rx]


def extract_arrow(mini_map: MatLike):
    """
    提取小箭头部分 范围越小越精准
    :param mini_map: 小地图
    :return: 小箭头
    """
    return cv2_utils.color_similarity_2d(mini_map, constants.COLOR_ARROW_BGR)


def get_arrow_mask(mm: MatLike):
    """
    获取小地图的小箭头掩码
    :param mm: 小地图
    :return: 中心区域的掩码 和 整张图的掩码
    """
    w, h = mm.shape[1], mm.shape[0]
    cx, cy = w // 2, h // 2
    d = constants.TEMPLATE_ARROW_LEN
    r = constants.TEMPLATE_ARROW_R
    center = mm[cy - r:cy + r, cx - r:cx + r]
    arrow = extract_arrow(center)
    _, mask = cv2.threshold(arrow, 180, 255, cv2.THRESH_BINARY)
    # 做一个连通性检测 小于50个连通的认为是噪点
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    large_components = []
    for label in range(1, num_labels):
        if stats[label, cv2.CC_STAT_AREA] < 50:
            large_components.append(label)
    for label in large_components:
        mask[labels == label] = 0

    whole_mask = np.zeros((h,w), dtype=np.uint8)
    whole_mask[cy-r:cy+r, cx-r:cx+r] = mask
    # 黑色边缘线条采集不到 稍微膨胀一下
    kernel = np.ones((5, 5), np.uint8)
    cv2.dilate(src=whole_mask, dst=whole_mask, kernel=kernel, iterations=1)
    arrow_mask, _ = cv2_utils.convert_to_standard(mask, mask, width=d, height=d)
    return arrow_mask, whole_mask


def get_angle_from_arrow(arrow: MatLike,
                         im: ImageMatcher,
                         show: bool = False) -> int:
    """
    用小地图上的箭头 计算当前方向 正右方向为0度 逆时针旋转为正度数
    :param arrow: 已经提取好的白色的箭头
    :param all_template: 模板 每5度一张图的模板
    :param one_template: 模板 0度的模板
    :param im: 图片匹配器
    :param show: 显示结果
    :return: 角度 正右方向为0度 顺时针旋转为正度数
    """
    rough_template = im.get_template('arrow_rough').mask
    result = im.match_image(rough_template, arrow, threshold=0.85)
    if len(result) == 0:
        return None

    if show:
        cv2_utils.show_image(rough_template, result.max, win_name="rough_template_match")

    d = constants.TEMPLATE_ARROW_LEN_PLUS

    row = result.max.cy // d
    col = result.max.cx // d
    rough_angle = (row * 11 + col) * 3

    rough_arrow = cv2_utils.image_rotate(arrow, -rough_angle)
    precise_template = im.get_template('arrow_precise').mask
    result2 = im.match_image(precise_template, rough_arrow, threshold=0.85)

    if len(result2) == 0:
        precise_angle = rough_angle
    else:
        row = result2.max.cy // d
        col = result2.max.cx // d
        precise_delta_angle = (row * 11 + col - 6) / 10.0
        precise_angle = rough_angle + precise_delta_angle

    if precise_angle is not None and precise_angle < 0:
        precise_angle += 360
    if precise_angle is not None and precise_angle > 360:
        precise_angle -= 360
    return 360 - precise_angle


def analyse_arrow_and_angle(mini_map: MatLike, im: ImageMatcher):
    """
    在小地图上获取小箭头掩码和角度
    :param mini_map: 小地图图片
    :param im: 图片匹配器
    :return:
    """
    center_arrow_mask, arrow_mask = get_arrow_mask(mini_map)
    angle = get_angle_from_arrow(center_arrow_mask, im)  # 正右方向为0度 顺时针旋转为正度数
    return center_arrow_mask, arrow_mask, angle


def get_edge_mask_by_hsv(mm: MatLike, arrow_mask: MatLike):
    """
    废弃了 背景亮的時候效果很差
    将小地图转化成HSV格式，然后根据亮度扣出前景
    最后用Canny画出边缘
    :param mm: 小地图截图
    :param arrow_mask: 小箭头掩码 传入时忽略掉这部分
    :return:
    """
    hsv = cv2.cvtColor(mm, cv2.COLOR_BGR2HSV)
    hsv_mask = cv2.threshold(hsv[:, :, 2], 180, 255, cv2.THRESH_BINARY)[1]
    if arrow_mask is not None:
        hsv_mask = cv2.bitwise_and(hsv_mask, hsv_mask, mask=cv2.bitwise_not(arrow_mask))

    # 膨胀一下 粗点的边缘可以抹平一些取色上的误差 后续模板匹配更准确
    kernel = np.ones((3, 3), np.uint8)
    return cv2.dilate(hsv_mask, kernel, iterations=1)


def get_sp_mask_by_feature_match(mm_info: MiniMapInfo, im: ImageMatcher,
                                 sp_types: Set = None,
                                 show: bool = False):
    """
    在小地图上找到特殊点
    使用特征匹配 每个模板最多只能找到一个
    :param mm_info: 小地图信息
    :param im: 图片匹配器
    :param sp_types: 限定种类的特殊点
    :param show: 是否展示结果
    :return:
    """
    source = mm_info.origin
    source_mask = mm_info.circle_mask
    source_kps, source_desc = cv2_utils.feature_detect_and_compute(source, mask=source_mask)

    sp_mask = np.zeros_like(mm_info.gray)
    sp_match_result = {}
    for prefix in ['mm_tp', 'mm_sp']:
        for i in range(100):
            if i == 0:
                continue

            template_id = '%s_%02d' % (prefix, i)
            t: TemplateImage = im.get_template(template_id)
            if t is None:
                break
            if sp_types is not None and template_id not in sp_types:
                continue

            match_result_list = MatchResultList()
            template = t.origin
            template_mask = t.mask

            template_kps, template_desc = t.kps, t.desc

            good_matches, offset_x, offset_y, scale = cv2_utils.feature_match(
                source_kps, source_desc,
                template_kps, template_desc,
                source_mask=source_mask)

            if offset_x is not None:
                mr = MatchResult(1, offset_x, offset_y, template.shape[1], template.shape[0], template_scale=scale)  #
                match_result_list.append(mr)
                sp_match_result[template_id] = match_result_list

                # 缩放后的宽度和高度
                sw = int(template.shape[1] * scale)
                sh = int(template.shape[0] * scale)
                one_sp_mask = cv2.resize(template_mask, (sw, sh))

                rect1, rect2 = cv2_utils.get_overlap_rect(sp_mask, one_sp_mask, mr.x, mr.y)
                sx_start, sy_start, sx_end, sy_end = rect1
                tx_start, ty_start, tx_end, ty_end = rect2
                sp_mask[sy_start:sy_end, sx_start:sx_end] = cv2.bitwise_or(
                    sp_mask[sy_start:sy_end, sx_start:sx_end],
                    one_sp_mask[ty_start:ty_end, tx_start:tx_end]
                )

            if show:
                cv2_utils.show_image(source, win_name='source')
                cv2_utils.show_image(source_mask, win_name='source_mask')
                source_with_keypoints = cv2.drawKeypoints(source, source_kps, None)
                cv2_utils.show_image(source_with_keypoints, win_name='source_with_keypoints_%s' % template_id)
                template_with_keypoints = cv2.drawKeypoints(template, template_kps, None)
                cv2_utils.show_image(
                    cv2.bitwise_and(template_with_keypoints, template_with_keypoints, mask=template_mask),
                    win_name='template_with_keypoints_%s' % template_id)
                all_result = cv2.drawMatches(template, template_kps, source, source_kps, good_matches, None, flags=2)
                cv2_utils.show_image(all_result, win_name='all_match_%s' % template_id)

                if offset_x is not None:
                    cv2_utils.show_overlap(source, template, offset_x, offset_y, template_scale=scale, win_name='overlap_%s' % template_id)
                cv2.waitKey(0)
                cv2.destroyAllWindows()

    return sp_mask, sp_match_result


def get_enemy_road_mask(mm: MatLike) -> MatLike:
    """
    在小地图上找红点敌人的道路掩码
    :param mm: 小地图截图
    :return: 敌人在小地图上的坐标
    """
    lower_color = np.array([45, 45, 80], dtype=np.uint8)
    upper_color = np.array([60, 60, 255], dtype=np.uint8)
    return cv2.inRange(mm, lower_color, upper_color)


def get_track_road_mask(mm: MatLike) -> MatLike:
    """
    小地图上移动轨迹的掩码
    :param mm: 小地图截图
    :return: 掩码
    """
    lower_color = np.array([45, 45, 110], dtype=np.uint8)
    upper_color = np.array([65, 65, 130], dtype=np.uint8)
    return cv2.inRange(mm, lower_color, upper_color)


def is_under_attack(mm: MatLike, mm_pos: MiniMapPos, show: bool = False) -> bool:
    """
    根据小地图边缘 判断是否被锁定
    红色色道应该有一个圆
    :param mm: 小地图截图
    :param mm_pos: 小地图坐标信息
    :return: 是否被锁定
    """
    w, h = mm.shape[1], mm.shape[0]
    cx, cy = w // 2, h // 2
    r = mm_pos.r

    circle_mask = np.zeros(mm.shape[:2], dtype=np.uint8)
    cv2.circle(circle_mask, (cx, cy), r, 255, 3)

    circle_part = cv2.bitwise_and(mm, mm, mask=circle_mask)

    # 提取红色部分
    lower_color = np.array([0, 0, 200], dtype=np.uint8)
    upper_color = np.array([100, 100, 255], dtype=np.uint8)
    red = cv2.inRange(circle_part, lower_color, upper_color)

    # 提取橙色部分
    lower_color = np.array([0, 150, 200], dtype=np.uint8)
    upper_color = np.array([100, 180, 255], dtype=np.uint8)
    orange = cv2.inRange(circle_part, lower_color, upper_color)

    mask = cv2.bitwise_or(red, orange)

    circles = cv2.HoughCircles(mask, cv2.HOUGH_GRADIENT, 0.3, 100, param1=10, param2=10,
                               minRadius=mm_pos.r - 10, maxRadius=mm_pos.r + 10)
    find: bool = circles is not None

    if show:
        cv2_utils.show_image(circle_part, win_name='circle_part')
        cv2_utils.show_image(red, win_name='red')
        cv2_utils.show_image(orange, win_name='orange')
        cv2_utils.show_image(mask, win_name='mask')
        find_circle = np.zeros(mm.shape[:2], dtype=np.uint8)
        if circles is not None:
            # 将检测到的圆形绘制在原始图像上
            circles = np.round(circles[0, :]).astype("int")
            for (x, y, r) in circles:
                cv2.circle(find_circle, (x, y), r, 255, 1)
        cv2_utils.show_image(find_circle, win_name='find_circle')

    return find


def with_enemy_in_main_road(mm: MatLike):
    """
    小地图当前楼层上是否有怪物
    只靠红色提取效果不太行 背景太杂了
    :param mm:
    :return:
    """
    b, g, r = cv2.split(mm)
    red_mask = np.zeros(mm.shape[:2], dtype=np.uint8)
    red_mask[np.where(r > 230)] = 255

    mm_pos = get_game_config().mini_map_pos
    circle_mask = np.zeros(mm.shape[:2], dtype=np.uint8)
    cx = mm.shape[1] // 2
    cy = mm.shape[0] // 2
    r = mm_pos.r - 10
    cv2.circle(circle_mask, (cx, cy), r, 255, -1)

    mask = cv2.bitwise_and(red_mask, circle_mask)
    cv2_utils.show_image(mm, win_name='mm')
    cv2_utils.show_image(mask, wait=0)
    return np.sum(mask == 255) > 10


def get_mini_map_scale_list(running: bool):
    return [1.25, 1.20, 1.15, 1.10] if running else [1, 1.05]


def analyse_mini_map(origin: MatLike, im: ImageMatcher, sp_types: Set = None,
                     another_floor: bool = True) -> MiniMapInfo:
    """
    预处理 从小地图中提取出所有需要的信息
    :param origin: 小地图 左上角的一个正方形区域
    :param im: 图片匹配器
    :param sp_types: 特殊点种类
    :param another_floor: 是否有其它楼层
    :return:
    """
    info = MiniMapInfo()
    info.origin = origin
    info.center_arrow_mask, info.arrow_mask, info.angle = analyse_arrow_and_angle(origin, im)
    info.gray = cv2.cvtColor(origin, cv2.COLOR_BGR2GRAY)

    # 小地图要只判断中间正方形 圆形边缘会扭曲原来特征
    h, w = origin.shape[1], origin.shape[0]
    cx, cy = w // 2, h // 2
    r = math.floor(h / math.sqrt(2) / 2) - 8
    info.center_mask = np.zeros_like(info.gray)
    info.center_mask[cy - r:cy + r, cx - r:cx + r] = 255
    info.center_mask = cv2.bitwise_xor(info.center_mask, info.arrow_mask)

    info.circle_mask = np.zeros_like(info.gray)
    cv2.circle(info.circle_mask, (cx, cy), h // 2 - 5, 255, -1)  # 忽略一点圆的边缘
    info.circle_mask = cv2.bitwise_xor(info.circle_mask, info.arrow_mask)

    info.sp_mask, info.sp_result = get_sp_mask_by_feature_match(info, im, sp_types)
    info.road_mask = get_mini_map_road_mask(origin, sp_mask=info.sp_mask, arrow_mask=info.arrow_mask, angle=info.angle,
                                            another_floor=another_floor)
    info.gray, info.feature_mask = merge_all_map_mask(info.gray, info.road_mask, info.sp_mask)

    info.edge = find_mini_map_edge_mask(origin, info.road_mask)

    # 特征点需要跟大地图的特征点获取方式一致 见 large_map.init_large_map
    info.kps, info.desc = cv2_utils.feature_detect_and_compute(info.gray, mask=info.sp_mask)

    return info


def get_mini_map_road_mask(origin: MatLike,
                           sp_mask: MatLike = None,
                           arrow_mask: MatLike = None,
                           angle: int = None,
                           another_floor: bool = True) -> MatLike:
    """
    在地图中 按接近道路的颜色圈出地图的主体部分 过滤掉无关紧要的背景
    :param origin: 地图图片
    :param sp_mask: 特殊点的掩码 道路掩码应该排除这部分
    :param arrow_mask: 小箭头的掩码 只有小地图有
    :param angle: 只有小地图上需要传入 表示当前朝向
    :param another_floor: 是否有其它楼层
    :return: 道路掩码图 能走的部分是白色255
    """
    # 按道路颜色圈出 当前层的颜色
    l1 = 45
    u1 = 65
    lower_color = np.array([l1, l1, l1], dtype=np.uint8)
    upper_color = np.array([u1, u1, u1], dtype=np.uint8)
    road_mask_1 = cv2.inRange(origin, lower_color, upper_color)
    if another_floor:  # 如果区域中包含其它图层 则考虑其它图层的颜色
        # 按道路颜色圈出 其他层的颜色
        l2 = 60
        u2 = 75
        lower_color = np.array([l2, l2, l2], dtype=np.uint8)
        upper_color = np.array([u2, u2, u2], dtype=np.uint8)
        road_mask_2 = cv2.inRange(origin, lower_color, upper_color)

        road_mask = cv2.bitwise_or(road_mask_1, road_mask_2)
    else:
        road_mask = road_mask_1

    # 对于小地图 要特殊扫描中心点附近的区块
    radio_mask = get_mini_map_radio_mask(origin, angle, another_floor=another_floor)
    # cv2_utils.show_image(radio_mask, win_name='radio_mask')
    center_mask = cv2.bitwise_or(arrow_mask, radio_mask)
    road_mask = cv2.bitwise_or(road_mask, center_mask)

    # 特殊处理敌人红点
    enemy_mask = get_enemy_road_mask(origin)
    road_mask = cv2.bitwise_or(road_mask, enemy_mask)

    # 合并特殊点进行连通性检测
    to_check_connection = cv2.bitwise_or(road_mask, sp_mask) if sp_mask is not None else road_mask
    # cv2_utils.show_image(to_check_connection, win_name='to_check_connection')

    # 非道路连通块 < 50的，认为是噪点 加入道路
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(cv2.bitwise_not(to_check_connection), connectivity=4)
    large_components = []
    for label in range(1, num_labels):
        if stats[label, cv2.CC_STAT_AREA] < 50:
            large_components.append(label)
    for label in large_components:
        to_check_connection[labels == label] = 255

    # 找到多于500个像素点的连通道路 这些才是真的路
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(to_check_connection, connectivity=4)
    large_components = []
    for label in range(1, num_labels):
        if stats[label, cv2.CC_STAT_AREA] > 200:
            large_components.append(label)
    real_road_mask = np.zeros(origin.shape[:2], dtype=np.uint8)
    for label in large_components:
        real_road_mask[labels == label] = 255

    # 排除掉特殊点
    if sp_mask is not None:
        real_road_mask = cv2.bitwise_and(real_road_mask, cv2.bitwise_not(sp_mask))

    return real_road_mask


def get_mini_map_radio_mask(mm: MatLike, angle: int = None, another_floor: bool = True):
    """
    小地图中心雷达区的掩码
    :param mm: 小地图图片
    :param angle: 当前人物角度
    :param another_floor: 是否有其它楼层
    :return:
    """
    radio_mask = np.zeros(mm.shape[:2], dtype=np.uint8)  # 圈出雷达区的掩码
    center = (mm.shape[1] // 2, mm.shape[0] // 2)  # 中心点坐标
    radius = 55  # 扇形半径 这个半径内
    color = 255  # 扇形颜色（BGR格式）
    thickness = -1  # 扇形边框线宽度（负值表示填充扇形）
    if angle is not None:  # 知道当前角度的话 画扇形
        start_angle = angle - 45  # 扇形起始角度（以度为单位）
        end_angle = angle + 45  # 扇形结束角度（以度为单位）
        cv2.ellipse(radio_mask, center, (radius, radius), 0, start_angle, end_angle, color, thickness)  # 画扇形
    else:  # 圆形兜底
        cv2.circle(radio_mask, center, radius, color, thickness)  # 画扇形
    radio_map = cv2.bitwise_and(mm, mm, mask=radio_mask)
    # cv2_utils.show_image(radio_map, win_name='radio_map')
    # 当前层数的
    lower_color = np.array([70, 70, 45], dtype=np.uint8)
    upper_color = np.array([130, 130, 65], dtype=np.uint8)
    road_radio_mask_1 = cv2.inRange(radio_map, lower_color, upper_color)

    if another_floor:
        # 其他层数的
        lower_color = np.array([70, 70, 70], dtype=np.uint8)
        upper_color = np.array([130, 130, 85], dtype=np.uint8)
        road_radio_mask_2 = cv2.inRange(radio_map, lower_color, upper_color)

        road_radio_mask = cv2.bitwise_or(road_radio_mask_1, road_radio_mask_2)
    else:
        road_radio_mask = road_radio_mask_1

    return road_radio_mask


def merge_all_map_mask(gray_image: MatLike,
                       road_mask, sp_mask):
    """
    :param gray_image:
    :param road_mask:
    :param sp_mask:
    :return:
    """
    usage = gray_image.copy()

    # 稍微膨胀一下
    kernel = np.ones((5, 5), np.uint8)
    expand_sp_mask = cv2.dilate(sp_mask, kernel, iterations=1)

    all_mask = cv2.bitwise_or(road_mask, expand_sp_mask)
    usage[np.where(all_mask == 0)] = constants.COLOR_WHITE_GRAY
    usage[np.where(road_mask == 255)] = constants.COLOR_MAP_ROAD_GRAY
    return usage, all_mask


def find_mini_map_edge_mask(origin: MatLike, road_mask: MatLike):
    """
    小地图道路边缘掩码 暂时不需要
    :param origin: 小地图图片
    :param road_mask: 道路掩码
    :return:
    """
    lower_color = np.array([170, 170, 170], dtype=np.uint8)
    upper_color = np.array([230, 230, 230], dtype=np.uint8)
    edge_mask_1 = cv2.inRange(origin, lower_color, upper_color)
    lower_color = np.array([100, 100, 100], dtype=np.uint8)
    upper_color = np.array([130, 130, 130], dtype=np.uint8)
    edge_mask_2 = cv2.inRange(origin, lower_color, upper_color)

    color_edge_mask = cv2.bitwise_or(edge_mask_1, edge_mask_2)
    # 稍微膨胀一下
    color_edge_mask = cv2.dilate(color_edge_mask, np.ones((5, 5), np.uint8), iterations=1)

    road_edge_mask = cv2.Canny(road_mask, threshold1=200, threshold2=230)
    color_edge_mask = cv2.dilate(color_edge_mask, np.ones((2, 2), np.uint8), iterations=1)

    final_edge_mask = cv2.bitwise_and(color_edge_mask, road_edge_mask)

    return final_edge_mask