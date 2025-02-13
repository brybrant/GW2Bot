import base64
import collections
import io
import itertools
import math
import re
import struct

import discord
import requests
from discord.ext import commands
from discord import app_commands
from discord.app_commands import Choice
from PIL import Image, ImageDraw

from .utils.chat import cleanup_xml_tags, embed_list_lines
from .utils.db import prepare_search

CHATCODE_REGEX = re.compile(r"\[\&(?=[^\s\[\]]*\])(.*?)\]")
TILESERVICE_BASE_URL = "https://tiles.guildwars2.com/"


class Build:
    def __init__(self, cog, profession, specializations, skills, code):
        self.cog = cog
        self.specializations = specializations
        self.skills = skills
        self.profession = profession
        self.code = code

    @classmethod
    async def from_code(cls, cog, chatcode):
        code = chatcode[2:-1]
        code = base64.b64decode(code)
        fields = struct.unpack("8B10H4B6H", code)
        profession_doc = await cog.db.professions.find_one({"code": fields[1]})
        specializations = []
        for spec, traits in zip(*[iter(fields[2:8])] * 2):
            if spec == 0:
                continue
            bit_string = "{:3b}".format(traits)
            bit_string = bit_string.strip()
            bit_string = bit_string.zfill(6)
            indexes = ([
                int(bit_string[i:i + 2], 2) - 1 for i in range(0, 6, 2)
            ])
            indexes.reverse()
            spec_doc = await cog.db.specializations.find_one({"_id": spec})
            indexes = [
                t + i for t, i in zip(indexes, range(0, 9, 3)) if t >= 0
            ]
            active_traits = []
            for i in indexes:
                active_traits.append(spec_doc["major_traits"][i])
            trait_docs = {}
            for trait in spec_doc["minor_traits"] + spec_doc["major_traits"]:
                trait_docs[trait] = await cog.db.traits.find_one(
                    {"_id": trait})
            specializations.append({
                "spec_doc": spec_doc,
                "active_traits": active_traits,
                "trait_docs": trait_docs
            })
        skill_ids = []
        skills = []
        if profession_doc["_id"] == "Ranger":
            for pet in [fields[18], fields[19]]:
                pet = await cog.db.pets.find_one({"_id": pet})
                skills.append(pet)
        if profession_doc["_id"] == "Revenant":
            for legend in [fields[18], fields[19]]:
                legend_doc = await cog.db.legends.find_one({"code": legend})
                skill_ids.append(legend_doc["swap"])
        else:
            palettes = []
            for skill in fields[8:18:2]:
                palettes.append(skill)
            for palette in palettes:
                for palette_id, skill_id in profession_doc[
                        "skills_by_palette"]:
                    if palette == palette_id:
                        skill_ids.append(skill_id)
                        break
        for skill_id in skill_ids:
            skills.append(await cog.db.skills.find_one({"_id": skill_id}))
        profession = await cog.get_profession(
            profession_doc["name"], [x["spec_doc"] for x in specializations])
        return cls(cog, profession, specializations, skills, chatcode)

    @classmethod
    async def from_build_tab(cls, cog, build_tab):
        profession_doc = await cog.db.professions.find_one(
            {"_id": build_tab["build"]["profession"]})

        async def get_skills(tab, terrestrial=True):
            skills_key = "skills"
            if not terrestrial:
                skills_key = f"aquatic_{skills_key}"
            skills = tab[skills_key]
            skill_docs = []
            skill_ids = []
            legend_docs = []
            swap_skill_docs = []
            pet_docs = []
            for skill in skills.values():
                if isinstance(skill, list):
                    skill_ids += skill
                    continue
                skill_ids.append(skill)
            for skill_id in skill_ids:
                skill_doc = await cog.db.skills.find_one({"_id": skill_id})
                if not skill_doc:
                    continue
                for palette_id, skill_id_2 in profession_doc[
                        "skills_by_palette"]:
                    if skill_id == skill_id_2:
                        skill_doc["palette_id"] = palette_id
                        break
                skill_docs.append(skill_doc)
            legends_key = "legends"
            if not terrestrial:
                legends_key = f"aquatic_{legends_key}"
            legends = tab.get(legends_key)
            if legends:
                for legend in legends:
                    if legend:
                        legend_doc = await cog.db.legends.find_one(
                            {"_id": legend})
                        if not legend_doc:
                            continue
                        swap_skill_docs.append(await cog.db.skills.find_one(
                            {"_id": legend_doc["swap"]}))
                        utility_palettes = []
                        for utility_skill in legend_doc["utilities"]:
                            for palette_id, skill_id_2 in profession_doc[
                                    "skills_by_palette"]:
                                if skill_id == skill_id_2:
                                    skill_doc["palette_id"] = palette_id
                                    break
                        legend_doc["utility_palettes"] = utility_palettes
                        legend_docs.append(legend_doc)
            pets = tab.get("pets")
            if pets:
                key = "terrestrial" if terrestrial else "aquatic"
                for pet in pets[key]:
                    if pet:
                        pet_docs.append(await
                                        cog.db.pets.find_one({"_id": pet}))

            Skills = collections.namedtuple(
                "Skills",
                ["skill_docs", "legend_docs", "swap_skill_docs", "pet_docs"])
            return Skills(skill_docs, legend_docs, swap_skill_docs, pet_docs)

        build = build_tab["build"]
        specializations = build["specializations"]
        if not specializations:
            return None
        specs = []
        for spec in specializations:
            if not spec:
                continue
            if spec["id"] == 0:
                continue
            spec_doc = await cog.db.specializations.find_one(
                {"_id": spec["id"]})
            if not spec_doc:
                continue
            trait_docs = {}
            for trait in spec_doc["minor_traits"] + spec_doc["major_traits"]:
                trait_docs[trait] = await cog.db.traits.find_one(
                    {"_id": trait})
            specs.append({
                "spec_doc": spec_doc,
                "trait_docs": trait_docs,
                "active_traits": spec["traits"]
            })
        profession = await cog.get_profession(build["profession"],
                                              [x["spec_doc"] for x in specs])
        profession_code = profession_doc["code"]
        terrestrial = await get_skills(build)
        aquatic = await get_skills(build, False)
        fields = [13, profession_code]
        for spec in specs + [None] * (3 - len(specs)):
            if not spec:
                fields += [0] * 2
                continue
            fields.append(spec["spec_doc"]["_id"])
            bit_string = "0" * 6
            for trait in reversed(spec["active_traits"]):
                try:
                    index = spec["spec_doc"]["major_traits"].index(trait) % 3
                    index += 1
                except ValueError:
                    index = 0
                bit_string += f"{index:02b}"
            bit_string = bit_string.zfill(6)
            fields.append(int(bit_string, 2))
        terrestrial_palettes = [
            skill["palette_id"] for skill in terrestrial.skill_docs
            if "palette_id" in skill
        ]
        aquatic_palettes = [
            skill["palette_id"] for skill in aquatic.skill_docs
            if "palette_id" in skill
        ]
        terrestrial_palettes += [0] * (5 - len(terrestrial_palettes))
        aquatic_palettes += [0] * (5 - len(aquatic_palettes))
        palettes = list(
            itertools.chain(*zip(terrestrial_palettes, aquatic_palettes)))
        fields += palettes
        if profession_doc["_id"] == "Ranger":
            terrestrial_pets = [pet["_id"] for pet in terrestrial.pet_docs]
            aquatic_pets = [pet["_id"] for pet in aquatic.pet_docs]
            terrestrial_pets += [0] * (2 - len(terrestrial_pets))
            aquatic_pets += [0] * (2 - len(aquatic_pets))
            fields += terrestrial_pets
            fields += aquatic_pets
        if profession_doc["_id"] == "Revenant":
            terrestrial_legend_codes = [
                leg["code"] for leg in terrestrial.legend_docs
            ]
            aquatic_legend_codes = [leg["code"] for leg in aquatic.legend_docs]
            terrestrial_legend_codes += [0] * (2 -
                                               len(terrestrial_legend_codes))
            aquatic_legend_codes += [0] * (2 - len(aquatic_legend_codes))
            fields += terrestrial_legend_codes + aquatic_legend_codes
            # TODO add inactive legend palettes
        fields += [0] * (28 - len(fields))
        code = struct.pack("8B10H4B6H", *fields)
        code = base64.b64encode(code)
        code = code.decode()
        code = f"[&{code}]"
        skills = terrestrial.skill_docs
        if profession_doc["_id"] == "Revenant":
            skills = terrestrial.swap_skill_docs
        if profession_doc["_id"] == "Ranger":
            skills = terrestrial.pet_docs + terrestrial.skill_docs

        return cls(cog, profession, specs, skills, code)

    def __render(self, filename):
        if not self.skills and not self.specializations:
            return None
        session = requests.Session()
        image = None
        draw = None
        skills_size = 64 if self.skills else 0
        for index, d in enumerate(self.specializations):
            spec_image = self.render_specialization(d["spec_doc"],
                                                    d["active_traits"],
                                                    d["trait_docs"], session)
            if not image:
                image = Image.new(
                    "RGBA", (spec_image.width, skills_size +
                             (spec_image.height * len(self.specializations))))
                draw = ImageDraw.ImageDraw(image)
            image.paste(spec_image,
                        (0, skills_size + (spec_image.height * index)))
            draw.text((5, (spec_image.height * index) + spec_image.height -
                       35 + skills_size),
                      d["spec_doc"]["name"],
                      fill="#FFFFFF",
                      font=self.cog.font)
        #try:
        crop_amount = 6
        if not image:
            image = Image.new("RGBA", (645, skills_size))
        for i, skill in enumerate(self.skills, start=0):
            resp = session.get(skill["icon"])
            skill_icon = Image.open(io.BytesIO(resp.content))
            skill_icon = skill_icon.resize((64, 64), Image.ANTIALIAS)
            skill_icon = skill_icon.crop(
                (crop_amount, crop_amount, skill_icon.width - crop_amount,
                 skill_icon.height - crop_amount))
            space_used = skill_icon.width * len(self.skills)
            empty_space = image.width - space_used
            spacing = empty_space // (len(self.skills) + 1)
            width = (i * skill_icon.width) + ((i + 1) * spacing)
            image.paste(skill_icon, ((width), 5))

    #   except Exception as e:
    #      self.cog.log.exception("Exception displayking skills: ",
    #                           exc_info=e)
        session.close()
        output = io.BytesIO()
        image.save(output, "png")
        output.seek(0)
        file = discord.File(output, filename)
        return file

    async def render(self, *, filename="specializations.png"):
        return await self.cog.bot.loop.run_in_executor(None, self.__render,
                                                       filename)

    @staticmethod
    def render_specialization(specialization, active_traits, trait_docs,
                              session):
        def get_trait_image(icon_url, size):
            resp = session.get(icon_url)
            image = Image.open(io.BytesIO(resp.content))
            image = image.crop((4, 4, image.width - 4, image.height - 4))
            return image.resize((size, size), Image.ANTIALIAS)

        resp = session.get(specialization["background"])
        background = Image.open(io.BytesIO(resp.content))
        background = background.crop((0, 121, 645, 256))
        draw = ImageDraw.ImageDraw(background)
        polygon_points = [
            120, 11, 167, 39, 167, 93, 120, 121, 73, 93, 73, 39, 120, 11
        ]
        draw.line(polygon_points, fill=(183, 190, 195), width=3)
        mask = Image.new("RGBA", background.size, color=(0, 0, 0, 135))
        d = ImageDraw.ImageDraw(mask)
        d.polygon(polygon_points, fill=(0, 0, 0, 0))
        background.paste(mask, mask=mask)
        mask.close()
        column = 0
        size = (background.height - 18) // 3
        trait_mask = Image.new("RGBA", (size, size), color=(0, 0, 0, 135))
        for index, trait in enumerate(specialization["major_traits"]):
            trait_doc = trait_docs[trait]
            image = get_trait_image(trait_doc["icon"], size)
            if trait not in active_traits:
                image.paste(trait_mask, mask=trait_mask)
            background.paste(image, (272 + (column * 142), 6 +
                                     ((size + 3) * (index % 3))), image)
            if index and not (index + 1) % 3:
                column += 1
            image.close()
        trait_mask.close()
        minor_trait_mask = Image.new("RGBA", (size, size))
        d = ImageDraw.ImageDraw(minor_trait_mask)
        d.polygon([13, 2, 25, 2, 35, 12, 35, 27, 21, 36, 17, 36, 3, 27, 3, 12],
                  fill=(0, 0, 0, 255))
        for index, trait in enumerate(specialization["minor_traits"]):
            trait_doc = trait_docs[trait]
            image = get_trait_image(trait_doc["icon"], size)
            background.paste(image,
                             (272 - size - 32 + (index * 142), 6 + size + 3),
                             minor_trait_mask)
            image.close()
        minor_trait_mask.close()
        return background


class SkillsMixin:
    async def skill_autocomplete(self,
                                         interaction: discord.Interaction,
                                         current: str):
        if not current:
            return []
        query = prepare_search(current)
        query = {
            "name": query,  "professions": {"$ne": None}
        }
        items = await self.db.skills.find(query).to_list(25)
        return [Choice(name=it["name"], value=str(it["_id"])) for it in items]

    async def trait_autocomplete(self,
                                         interaction: discord.Interaction,
                                         current: str):
        if not current:
            return []
        query = prepare_search(current)
        query = {
            "name": query
        }
        items = await self.db.traits.find(query).to_list(25)
        return [Choice(name=it["name"], value=str(it["_id"])) for it in items]

    @app_commands.command(name="skill")
    @app_commands.describe(skill="The skill name to search for. "
    "Example: Meteor Shower.")
    @app_commands.autocomplete(skill=skill_autocomplete)
    async def skillinfo(self, interaction : discord.Interaction, skill: str):
        """Information about a given skill"""
        try:
            skill_id = int(skill)
        except ValueError:
            try:
                choices = await self.skill_autocomplete(interaction, skill)
                skill_id = int(choices[0].value)
            except (ValueError, IndexError):
                return await interaction.followup.send(
                    "Could not find any skills with that name.")
        await interaction.response.defer()
        choice = await self.db.skills.find_one({"_id": skill_id})
        data = await self.skill_embed(choice, interaction)
        await interaction.followup.send(embed=data)

    @app_commands.command(name="trait")
    @app_commands.describe(trait="The trait name to search for. Example: Fresh Air")
    @app_commands.autocomplete(trait=trait_autocomplete)
    async def traitinfo(self, interaction: discord.Interaction, trait : str):
        """Information about a given trait"""
        await interaction.response.defer()
        try:
            trait_id = int(trait)
        except ValueError:
            try:
                choices = await self.trait_autocomplete(interaction, trait)
                trait_id = int(choices[0].value)
            except (ValueError, IndexError):
                return await interaction.followup.send(
                    "Could not find any traits with that name.")
        choice = await self.db.traits.find_one({"_id" : trait_id})
        data = await self.skill_embed(choice, interaction)
        await interaction.followup.send(embed=data)

    async def skill_embed(self, skill, ctx):
        def get_skill_type():
            slot = skill["slot"]
            if slot.startswith("Weapon"):
                weapon = skill["weapon_type"]
                return " {} skill {}".format(weapon, slot[-1])
            if slot.startswith("Utility"):
                return " Utility Skill"
            if slot.startswith("Profession"):
                return " Profession Skill {}".format(slot[-1])
            if slot.startswith("Pet"):
                return " Pet skill"
            if slot.startswith("Downed"):
                return " Downed skill {}".format(slot[-1])
            return " Utility Skill"

        def find_closest_emoji(field):
            best_match = ""
            field = field.replace(" ", "_").lower()
            for emoji in self.emojis:
                if emoji in field:
                    if len(emoji) > len(best_match):
                        best_match = emoji
            if best_match:
                return self.get_emoji(ctx, best_match)
            return ""

        def get_resource_name(prof):
            resource = None
            if "initiative" in skill:
                resource = "Initiative"
                value = skill["initiative"]
            elif "cost" in skill:
                if prof == "Warrior":
                    resource = "Adrenaline"
                if prof == "Revenant":
                    resource = "Energy"
                if prof == "Ranger":
                    resource = "Astral Force"
                value = skill["cost"]
            if resource:
                return {
                    "text": resource + " cost",
                    "value": value,
                    "type": "ResourceCost"
                }
            return None

        replacement_attrs = [("BoonDuration", "Concentration"),
                             ("ConditionDuration", "Expertise"),
                             ("ConditionDamage", "Condition Damage"),
                             ("CritDamage", "Ferocity")]
        description = None
        if "description" in skill:
            description = cleanup_xml_tags(skill["description"])
            for tup in replacement_attrs:
                description = re.sub(*tup, description)
        url = "https://wiki.guildwars2.com/wiki/" + skill["name"].replace(
            ' ', '_')
        async with self.session.head(url) as r:
            if not r.status == 200:
                url = None
        data = discord.Embed(title=skill["name"],
                             description=description,
                             url=url,
                             color=await self.get_embed_color(ctx))
        # TODO add profession colors and racial colors
        if "icon" in skill:
            data.set_thumbnail(url=skill["icon"])
        professions = skill.get("professions")
        resource = None
        if professions:
            if len(professions) == 1:
                prof = professions[0]
                resource = get_resource_name(prof)
                data.colour = discord.Color(
                    int(self.gamedata["professions"][prof.lower()]["color"],
                        16))
                data.set_footer(text=prof + get_skill_type(),
                                icon_url=self.get_profession_icon(prof))
        if "facts" in skill:
            if resource:
                skill["facts"].append(resource)
            facts = self.get_skill_fields(skill)
            lines = []
            for fact in facts:
                line = ""
                if fact.get("prefix"):
                    line += "{}{}".format(find_closest_emoji(fact["prefix"]),
                                          fact["prefix"])
                line += "{}{}".format(find_closest_emoji(fact["field"]),
                                      fact["field"])
                if fact.get("value"):
                    line += ": " + fact["value"]
                for tup in replacement_attrs:
                    line = re.sub(*tup, line)
                lines.append(line)
            data = embed_list_lines(data, lines, "Tooltip")
        return data

    def get_skill_fields(self, skill):
        def calculate_damage(fact):
            weapon = skill.get("weapon_type")
            default = 690.5
            base_damage = None
            if weapon:
                weapon = weapon.lower()
                damage_groups = {
                    952.5: [
                        "axe", "dagger", "mace", "pistol", "scepter", "spear",
                        "trident", "speargun", "aquatic", "shortbow", "sword"
                    ],
                    857.5: ["focus", "shield", "torch"],
                    857: ["warhorn"],
                    1047.5: ["greatsword"],
                    1048: ["staff", "hammer"],
                    1000: ["longbow"],
                    1095.5: ["rifle"]
                }
                for group, weapons in damage_groups.items():
                    if weapon in weapons:
                        base_damage = group
                        break
            if not base_damage:
                base_damage = default
            hits = fact["hit_count"]
            multiplier = fact["dmg_multiplier"]
            return math.ceil(hits *
                             round(base_damage * 1000 * multiplier / 2597))

        fields = []
        order = [
            "Recharge", "ResourceCost", "Damage", "Percent", "AttributeAdjust",
            "BuffConversion", "Buff", "PrefixedBuff", "Number", "Radius",
            "Duration", "Time", "Distance", "ComboField", "Heal",
            "HealingAdjust", "NoData", "Unblockable", "Range", "ComboFinisher",
            "StunBreak"
        ]
        for fact in sorted(skill["facts"],
                           key=lambda x: order.index(x["type"])):
            fact_type = fact["type"]
            text = fact.get("text", "")
            if fact_type == "Recharge":
                fields.append({
                    "field": text,
                    "value": "{}s".format(fact["value"])
                })
                continue
            if fact_type == "ResourceCost":
                fields.append({"field": text, "value": str(fact["value"])})
                continue
            if fact_type == "BuffConversion":
                fields.append({
                    "field":
                    "Gain {} based on a Percentage of {}".format(
                        fact["target"], fact["source"]),
                    "value":
                    "{}%".format(fact["percent"])
                })
            if fact_type == "Damage":
                damage = calculate_damage(fact)
                value = "{} ({})".format(
                    damage, round(fact["dmg_multiplier"] * fact["hit_count"],
                                  2))
                count = fact["hit_count"]
                if count > 1:
                    text += " ({}x)".format(count)
                fields.append({"field": text, "value": value})
                continue
            if fact_type == "AttributeAdjust":
                if not text:
                    text = fact.get("target", "")
                fields.append({
                    "field": text,
                    "value": "{:,}".format(fact["value"])
                })
                continue
            if fact_type == "PrefixedBuff":
                count = fact.get("apply_count")
                status = fact.get("status", "")
                duration = fact.get("duration")
                field = " " + status
                if duration:
                    field += "({}s)".format(duration)
                prefix = fact["prefix"].get("status")
                if prefix:
                    prefix += " "
                if not count:
                    fields.append({
                        "field": status,
                        "value": "Condition Removed",
                        "prefix": prefix
                    })
                    continue
                fields.append({
                    "field": field,
                    "value": fact.get("description"),
                    "prefix": prefix
                })
                continue
            if fact_type == "Buff":
                count = fact.get("apply_count")
                if not count:
                    fields.append({
                        "field": "{status}".format(**fact),
                        "value": "Condition Removed"
                    })
                    continue
                fields.append({
                    "field":
                    "{} {status}({duration}s)".format(count, **fact),
                    "value":
                    fact.get("description")
                })
                continue
            if fact_type == "Buff":
                count = fact.get("apply_count")
                if not count:
                    fields.append({
                        "field": "{status}".format(**fact),
                        "value": "Condition Removed"
                    })
                    continue
                fields.append({
                    "field":
                    "{} {status}({duration}s)".format(count, **fact),
                    "value":
                    fact["description"]
                })
                continue
            if fact_type == "Number":
                fields.append({"field": text, "value": str(fact["value"])})
                continue
            if fact_type == "Time":
                fields.append({
                    "field": text,
                    "value": "{}s".format(fact["duration"])
                })
                continue
            if fact_type in "Duration":
                fields.append({
                    "field": text,
                    "value": "{}s".format(fact["duration"])
                })
                continue
            if fact_type == "Radius":
                fields.append({
                    "field": text,
                    "value": "{:,}".format(fact["distance"])
                })
                continue
            if fact_type == "ComboField":
                fields.append({"field": text, "value": fact["field_type"]})
                continue
            if fact_type == "ComboFinisher":
                value = fact["finisher_type"]
                percent = fact["percent"]
                if not percent == 100:
                    value += " ({}%)".format(percent)
                fields.append({"field": text, "value": value})
                continue
            if fact_type == "Distance":
                fields.append({
                    "field": text,
                    "value": "{:,}".format(fact["distance"])
                })
                continue
            if fact_type in ("Heal", "HealingAdjust"):
                fields.append({"field": text, "value": str(fact["hit_count"])})
                continue
            if fact_type == "NoData":
                fields.append({
                    "field": text,
                })
                continue
            if fact_type == "Unblockable":
                fields.append({"field": "Unblockable"})
                continue
            if fact_type == "StunBreak":
                fields.append({"field": "Breaks stun"})
                continue
            if fact_type == "Percent":
                fields.append({
                    "field": text,
                    "value": "{}%".format(fact["percent"])
                })
                continue
            if fact_type == "Range":
                fields.append({
                    "field": text,
                    "value": "{:,}".format(fact["value"])
                })
                continue
        return fields


    async def prepare_linkpreview_guild_cache(self):
        cursor = self.bot.database.iter("guilds",
                                        {"link_preview_disabled": True}, self)
        async for doc in cursor:
            self.chatcode_preview_opted_out_guilds.add(doc["_id"])

    async def get_wiki_url(self, name):
        url = "https://wiki.guildwars2.com/wiki/" + name.replace(' ', '_')
        async with self.session.head(url) as r:
            if not r.status == 200:
                url = None
        return url

    @commands.Cog.listener("on_message")
    async def find_chatcodes(self, message : discord.Message):
        if message.guild:
            if message.guild.id in self.chatcode_preview_opted_out_guilds:
                return
        if not message.content:
            return
        if message.author.bot:
            return
        match = re.search(CHATCODE_REGEX, message.content)
        if not match:
            return
        chatcode = match.group()
        try:
            data = base64.b64decode(chatcode)
            header = struct.unpack("B", data[:1])
            header = header[0] - 1
            types = [
                "Coin", "Item", "NPC text string", "Map link", "PvP Game",
                "Skill", "Trait", "User", "Recipe", "Wardrobe", "Outfit",
                "WvW objective", "Build template"
            ]
            link_type = types[header]
            embed = discord.Embed(title=link_type, color=self.embed_color)
            embed.set_author(name=message.author.display_name,
                             icon_url=message.author.display_avatar.url)
            if message.guild:
                embed.set_footer(text=(
                    "Server admins can opt out of chat link "
                    "previewing by using the \"/server preview_chat_links\" command"),
                                 icon_url=self.bot.user.display_avatar.url)
            else:
                embed.set_footer(icon_url=self.bot.user.display_avatar.url)
            embed.description = "Chat link preview"
            reference = None
            if message.guild:
                me = message.guild.me
            else:
                me = self.bot.user
            if message.channel.permissions_for(me).read_message_history:
                reference = message
            match link_type:
                case "Coin":
                    # Currently disabled
                    return
                case "Item":
                    quantity, item_id = struct.unpack("<BI", data[1:5] + b"\0")
                    item_doc = await self.fetch_item(item_id)
                    if not item_doc:
                        return
                    embed.title = item_doc["name"]
                    embed.set_thumbnail(url=item_doc["icon"])
                    embed.color = int(
                        self.gamedata["items"]["rarity_colors"][
                            item_doc["rarity"]], 16)
                    suffix = ""
                    wiki_url = await self.get_wiki_url(item_doc["name"])
                    if wiki_url:
                        embed.url = wiki_url
                    if len(data) > 5:
                        bitfield = struct.unpack("<B", data[5:6])
                        bitfield = bitfield[0]
                        flags = []
                        # Wardrobe, upgrade 1, upgrade 2
                        for i in reversed(range(5, 8)):
                            flags.append(bool(bitfield >> i & 1))
                        if flags[2] and not flags[1]:  # Use first upgrade slut
                            flags[1] = True
                            flags[2] = False
                        offset = 0
                        if flags[0]:
                            skin_id = struct.unpack("<I", data[6:9] + b"\0")
                            skin_id = skin_id[0]
                            skin_doc = await self.db.skins.find_one(
                                {"_id": skin_id})
                            if not skin_doc:
                                name = "Unknown"
                            else:
                                name = skin_doc["name"]
                            embed.set_thumbnail(url=skin_doc["icon"])
                            embed.add_field(name="Skin", value=name)
                            offset += 4
                        upgrades = []
                        for upgrade in flags[1:]:
                            if not upgrade:
                                break
                            upgrade_id = struct.unpack(
                                "<I", data[6 + offset:9 + offset] + b"\0")
                            upgrade_id = upgrade_id[0]
                            upgrade_doc = await self.db.items.find_one(
                                {"_id": upgrade_id})
                            if not upgrade_doc:
                                upgrades.append("Unknown upgrade")
                                continue
                            else:
                                upgrades.append(upgrade_doc["name"])
                            if not suffix:
                                suffix = upgrade_doc["details"].get("suffix", "")
                            offset += 4
                        if upgrades:
                            field_name = "Upgrades" if len(
                                upgrades) > 1 else "Upgrade"
                            embed.add_field(name=field_name,
                                            value="\n".join(upgrades))
                        embed.title = f"{embed.title} {suffix}"
                    if quantity > 1:
                        embed.title = f"{quantity} {embed.title}"
                    return await message.channel.send(embed=embed, reference=reference, mention_author=False)
                case "NPC text string":
                    # Currently disabled
                    return
                case "Map link":
                    data = struct.unpack("<I", data[1:])
                    poi_id = data[0]
                    poi_doc = await self.db.pois.find_one({"_id": poi_id})
                    # continent_id = poi_doc["continent_id"]
                    # floor = poi_doc["floor"]
                    # x, y = [int(i) for i in poi_doc["coord"]]
                    # tile_url = TILESERVICE_BASE_URL + f"{continent_id}/{floor}/3/{x}/{y}.jpg"
                    # async with self.session.get(tile_url) as r:
                    #     image = io.BytesIO(await r.read())
                    # image.seek(0)
                    # print(tile_url)
                    # file = discord.File(image, "tile.jpg")
                    # embed.set_image(url=f"attachment://{file.filename}")
                    # await message.channel.send(file=file)
                    # API SUCKS
                    # TODO More detail
                    poi_type = poi_doc["type"].title()
                    if poi_type == "Landmark":
                        poi_type = "Point of Interest"
                    emoji = self.get_emoji(message, poi_type)
                    embed.add_field(name=emoji + poi_type,
                                    value=poi_doc.get("name", "Unnamed"))
                    return await message.channel.send(embed=embed, reference=reference, mention_author=False)
                case "PvP Game":
                    # Can't do much here
                    return
                case "Skill":
                    data = struct.unpack("<I", data[1:])
                    skill_id = data[0]
                    skill_doc = await self.db.skills.find_one({"_id": skill_id})
                    if not skill_doc:
                        return
                    new_embed = await self.skill_embed(skill_doc, message)
                    new_embed.set_footer(text=embed.footer.text,
                                        icon_url=embed.footer.icon_url)
                    return await message.channel.send(embed=new_embed, reference=reference, mention_author=False)
                case "Trait":
                    data = struct.unpack("<I", data[1:])
                    trait_id = data[0]
                    trait_doc = await self.db.traits.find_one({"_id": trait_id})
                    if not trait_doc:
                        return
                    new_embed = await self.skill_embed(trait_doc, message)
                    new_embed.set_footer(text=embed.footer.text,
                                         icon_url=embed.footer.icon_url)
                    return await message.channel.send(embed=new_embed, reference=reference, mention_author=False)
                case "User":
                    # Can't do much
                    return
                case "Recipe":
                    data = struct.unpack("<I", data[1:])
                    recipe_id = data[0]
                    recipe_doc = await self.db.recipes.find_one({"_id": recipe_id})
                    if not recipe_doc:
                        return
                    output = await self.fetch_item(recipe_doc["output_item_id"])
                    if output:
                        count = recipe_doc["output_item_count"]
                        name = output["name"]
                        embed.title = f"Recipe: {count} {name}"
                    emojis = message.channel.permissions_for(me).external_emojis
                    disciplines = recipe_doc.get("disciplines", [])
                    if emojis:
                        value = []
                        for disc in disciplines:
                            value.append(self.get_emoji(message, disc))
                        value = "".join(value)
                    else:
                        value = "\n".join(disciplines)
                    if value:
                        embed.add_field(name="Crafting disciplines", value=value)
                    ingredients = recipe_doc.get("ingredients", [])
                    value = []
                    for ingredient in ingredients:
                        item_doc = await self.fetch_item(ingredient["item_id"])
                        if item_doc:
                            name = item_doc["name"]
                            count = ingredient["count"]
                            value.append(f"{count} {name}")
                    value = "\n".join(value)
                    if value:
                        embed.add_field(name="Ingredients", value=value)
                    return await message.channel.send(embed=embed, reference=reference, mention_author=False)
                case "Wardrobe":
                    data = struct.unpack("<I", data[1:])
                    skin_id = data[0]
                    skin_doc = await self.db.skins.find_one({"_id": skin_id})
                    if not skin_doc:
                        return
                    embed.set_thumbnail(url=skin_doc["icon"])
                    embed.title = skin_doc["name"]
                    return await message.channel.send(embed=embed, reference=reference, mention_author=False)
                case "Outfit":
                    data = struct.unpack("<I", data[1:])
                    outfit_id = data[0]
                    outfit_doc = await self.db.outfits.find_one({"_id": outfit_id})
                    if not outfit_doc:
                        return
                    embed.set_thumbnail(url=outfit_doc["icon"])
                    embed.title = outfit_doc["name"]
                    return await message.channel.send(embed=embed, reference=reference, mention_author=False)
                case "WvW objective":
                    # TODO
                    return
                case "Build template":
                    build = await Build.from_code(self, chatcode)
                    file = await build.render()
                    embed.color = build.profession.color
                    embed.set_thumbnail(url=build.profession.icon)
                    embed.set_image(url=f"attachment://{file.filename}")
                    return await message.channel.send(embed=embed, reference=reference, file=file, mention_author=False)
        except Exception as e:
            self.log.exception(exc_info=e)
            pass
        finally:
            field_id = link_type.replace(" ", "_").lower()
            await self.bot.database.db.statistics.gw2.update_one(
                {"_id": "link_previews"}, {"$inc": {
                    field_id: 1
                }}, upsert=True)
