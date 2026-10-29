from daily_report.members import Member


def test_요약_이름은_표시_이름이고_비어_있으면_실명이다():
    assert Member("U1", display_name="길동", real_name="홍길동").name == "길동"
    assert Member("U1", display_name="", real_name="홍길동").name == "홍길동"


def test_Slack_사용자_정보에서_봇_게스트_비활성을_읽는다():
    guest = Member.from_slack(
        {"id": "U1", "is_restricted": True, "profile": {"display_name": "g", "real_name": "G"}}
    )
    single = Member.from_slack({"id": "U2", "is_ultra_restricted": True, "profile": {}})
    bot = Member.from_slack({"id": "B1", "is_bot": True, "profile": {}})
    slackbot = Member.from_slack({"id": "USLACKBOT", "profile": {}})
    gone = Member.from_slack({"id": "U3", "deleted": True, "profile": {}})

    assert guest.is_guest and guest.display_name == "g" and guest.real_name == "G"
    assert single.is_guest
    assert bot.is_bot and slackbot.is_bot
    assert gone.deleted
