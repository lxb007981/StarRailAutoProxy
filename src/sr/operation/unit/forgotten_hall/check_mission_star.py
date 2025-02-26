from typing import Callable

from cv2.typing import MatLike

from basic.i18_utils import gt
from basic.img import MatchResult
from sr.context import Context
from sr.operation import Operation, OperationOneRoundResult
from sr.operation.unit.forgotten_hall import get_mission_num_pos, get_mission_star_by_num_pos


class CheckMissionStar(Operation):

    def __init__(self, ctx: Context, mission_num: int, star_callback: Callable = None):
        """
        找到关卡的数字 判断下方的星星数量
        返回附加状态为星数
        :param ctx: 应用上下文
        :param mission_num: 扫描哪个关卡 1~10
        :param star_callback: 获取到星数后的回调
        """
        super().__init__(ctx, try_times=10, op_name='%s %d' % (gt('忘却之庭 获取关卡星数', 'ui'), mission_num))
        self.mission_num: int = mission_num
        self.star_callback: Callable = star_callback

    def _execute_one_round(self) -> OperationOneRoundResult:
        screen: MatLike = self.screenshot()

        num_result: MatchResult = get_mission_num_pos(self.ctx, self.mission_num, screen, drag_when_not_found=True)
        if num_result is None:
            return Operation.round_retry('未找到关卡')

        star: int = get_mission_star_by_num_pos(self.ctx, screen, num_result)

        if self.star_callback is not None:
            self.star_callback(self.mission_num, star)

        return Operation.round_success(str(star))
