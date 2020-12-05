import discord
import io
try:
    import matplotlib
    matplotlib.use("agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    import matplotlib.dates as mdates
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

from discord.ext import commands
from discord.ext.commands.cooldowns import BucketType

from .exceptions import APIError, APIKeyError
import datetime


def generate_population_graph(data):
    fig = plt.figure()
    path_effects = [pe.withStroke(linewidth=1, foreground="black")]
    ax = fig.add_subplot(111)
    ax.set_yticks([0, 1, 2, 3, 4])
    ax.set_yticklabels(["Low", "Medium", "High", "Very High", "Full"])
    ax.set_title('Population over time',
                 color="#ffa600",
                 path_effects=path_effects)
    ax.tick_params(axis='y', which='major', length=2)
    ax.step(*zip(*data), where="post", color="#c12d2b")
    ax.tick_params(axis="x", which="major", pad=0)
    ax.tick_params(axis="both", labelcolor="#ffa600", color="#c12d2b")
    ax.grid(b=True,
            which='both',
            color='black',
            linestyle='-',
            alpha=0.2,
            path_effects=[
                pe.withStroke(linewidth=1, foreground="white", alpha=0.2)
            ])
    ax.set_aspect(0.2 / ax.get_data_ratio())
    plt.setp([ax.get_xticklines(), ax.get_yticklines()], color="#ffa600")
    for spine in ax.spines.values():
        spine.set_edgecolor("#e7691e")
        spine.set_path_effects(path_effects)
    locator = mdates.AutoDateLocator(maxticks=10)
    formatter = mdates.ConciseDateFormatter(locator)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    for tick in ax.xaxis.get_major_ticks() + ax.yaxis.get_major_ticks():
        tick.label.set_path_effects(path_effects)
    buf = io.BytesIO()
    fig.savefig(buf,
                format="png",
                transparent=True,
                bbox_inches='tight',
                dpi=300)
    plt.close(fig)
    buf.seek(0)
    return buf


class WvwMixin:
    @commands.group(case_insensitive=True)
    async def wvw(self, ctx):
        """Commands related to WVW"""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @wvw.command(name="worlds")
    @commands.cooldown(1, 20, BucketType.user)
    async def wvw_worlds(self, ctx):
        """List all worlds"""
        try:
            results = await self.call_api("worlds?ids=all")
        except APIError as e:
            return await self.error_handler(ctx, e)
        output = "Available worlds are: ```"
        for world in results:
            output += world["name"] + ", "
        output += "```"
        await ctx.send(output)

    @wvw.command(name="info")
    @commands.cooldown(1, 10, BucketType.user)
    async def wvw_info(self, ctx, *, world: str = None):
        """Info about a world. Defaults to account's world
        """
        user = ctx.author
        if not world:
            try:
                endpoint = "account"
                results = await self.call_api(endpoint, user)
                wid = results["world"]
            except APIKeyError as e:
                return await ctx.send(
                    "No world name or key associated with your account")
            except APIError as e:
                return await self.error_handler(ctx, e)
        else:
            wid = await self.get_world_id(world)
        if not wid:
            await ctx.send("Invalid world name")
            return
        try:
            endpoints = [
                "wvw/matches?world={0}".format(wid),
                "worlds?id={0}".format(wid)
            ]
            matches, worldinfo = await self.call_multiple(endpoints)
        except APIError as e:
            return await self.error_handler(ctx, e)
        linked_worlds = []
        worldcolor = "green"
        for key, value in matches["all_worlds"].items():
            if wid in value:
                worldcolor = key
                value.remove(wid)
                linked_worlds = value
                break
        if worldcolor == "red":
            color = discord.Colour.red()
        elif worldcolor == "green":
            color = discord.Colour.green()
        else:
            color = discord.Colour.blue()
        linked_worlds = [await self.get_world_name(w) for w in linked_worlds]
        score = matches["scores"][worldcolor]
        ppt = 0
        victoryp = matches["victory_points"][worldcolor]
        for m in matches["maps"]:
            for objective in m["objectives"]:
                if objective["owner"].lower() == worldcolor:
                    ppt += objective["points_tick"]
        population = worldinfo["population"]
        if population == "VeryHigh":
            population = "Very high"
        kills = matches["kills"][worldcolor]
        deaths = matches["deaths"][worldcolor]
        kd = round((kills / deaths), 2)
        data = discord.Embed(description="Performance", colour=color)
        data.add_field(name="Score", value=score)
        data.add_field(name="Points per tick", value=ppt)
        data.add_field(name="Victory Points", value=victoryp)
        data.add_field(name="K/D ratio", value=str(kd), inline=False)
        data.add_field(name="Population", value=population)
        if linked_worlds:
            data.add_field(name="Linked with", value=", ".join(linked_worlds))
        data.set_author(name=worldinfo["name"])
        if MATPLOTLIB_AVAILABLE:
            graph = await self.get_population_graph(worldinfo)
            data.set_image(url=f"attachment://{graph.filename}")
            try:
                await ctx.send(embed=data, file=graph)
            except discord.Forbidden:
                await ctx.send("Missing permission to embed links or "
                               "upload images")
                # TODO automate giving exact error
            return
        try:
            await ctx.send(embed=data)
        except discord.Forbidden:
            await ctx.send("Need permission to embed links")

    @wvw.command(name="populationtrack")
    @commands.cooldown(1, 5, BucketType.user)
    async def wvw_population_track(self, ctx, *, world_name):
        """Receive a notification when the world is no longer full

        Example: $wvw populationtrack gandara
        """
        user = ctx.author
        wid = await self.get_world_id(world_name)
        if not wid:
            return await ctx.send("Invalid world name")
        doc = await self.bot.database.get_user(user, self)
        if doc and wid in doc.get("poptrack", []):
            return await ctx.send("You're already tracking this world")
        try:
            results = await self.call_api("worlds/{}".format(wid))
        except APIError as e:
            return await self.error_handler(ctx, e)
        if results["population"] != "Full":
            return await ctx.send("This world is currently not full!")
        try:
            await user.send("You will be notiifed when {} is no longer full "
                            "".format(world_name.title()))
        except:
            return await ctx.send("Couldn't send a DM to you. Either you have "
                                  "me blocked, or disabled DMs in this "
                                  "server. Aborting.")
        await self.bot.database.set_user(user, {"poptrack": wid},
                                         self,
                                         operator="$push")
        await ctx.send("Successfully set")

    def population_to_int(self, pop):
        pops = ["low", "medium", "high", "veryhigh", "full"]
        return pops.index(pop.lower().replace("_", ""))

    async def get_population_graph(self, world):
        cursor = self.db.worldpopulation.find({"world_id": world["id"]})
        data = []
        async for doc in cursor:
            data.append((doc["date"], doc["population"]))
        data.append((datetime.datetime.utcnow(),
                     self.population_to_int(world["population"])))
        data.sort(key=lambda x: x[0])
        graph = await self.bot.loop.run_in_executor(None,
                                                    generate_population_graph,
                                                    data)
        file = discord.File(graph, "graph.png")
        return file
